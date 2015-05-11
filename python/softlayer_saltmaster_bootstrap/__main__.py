#!/usr/bin/env python
# -*- coding: latin-1 -*-
"""
Program leverages softlayer python library and its credential file to manage a
SL VM containing a saltmaster instance.

TODO:
     - auto-retry datacenters
     - add a 'destructive' mode that will destroy existing instances and recreate them
     - add paramiko operation output

Notes:
     - SL cancel doesn't remove name of VM immediately, but does unset owner; should use to determine duplicate VMs and to do del, create operation
"""
__author__ = 'mdye <mdye@us.ibm.com>'
__version__ = '0.1.0'

import time
import sys
import os
import SoftLayer
import argparse
import tempfile
import tarfile
import copy
from pprint import pprint
import socket
import subprocess
from datetime import datetime
import paramiko
import traceback
from functools import partial

_debug = False
_client = SoftLayer.Client()

# script depends on Centos
_vm_template = {
            'startCpus': 1,
            'maxMemory': 1024,
            'hourlyBillingFlag': True,
            'operatingSystemReferenceCode': 'CENTOS_LATEST',
            'localDiskFlag': False,
            'datacenter': {
                'name': 'dal09'
            }
        }

class TimeLimitedOperationException(Exception):
    pass

def until_with_lim_test(test_fn, lim_min, desc, fn, *args, **kwargs):
    retries = 0
    start = datetime.now()
    test = False
    limit_reached = False

    while not (test or limit_reached):
        limit_reached = (datetime.now() - start).total_seconds() / 60 > lim_min
        res = fn(*args,**kwargs)
        retries = retries + 1
        _debug and print('operation {} retried: {} times'.format(desc, retries))
        if test_fn:
            test = test_fn(res)
        elif res:
            # no test_fn provided, if res is True, break out of retry loop
            test = True
            break;

        if retries > 2:
            time.sleep(3)

    if limit_reached:
        raise TimeLimitedOperationException('reached time limit {}m and didn\'t succeed'.format(lim_min))
    elif not test:
        raise Exception('failed operation-provided test')
    else:
        # success!
        _debug and print('succeeded, returning result')
        return res

until_with_lim = partial(until_with_lim_test, None)

def _print_ssh(ssh_io):
    stdin, stdout, stderr = ssh_io
    err = stderr.read()
    out = stdout.read()

    if err:
        print('*** SSH stderr ***\n{}'.format(err))
    if out:
        print('+ SSH stdout\n{}'.format(out))

def _locate_instance(name, domain, client):
    """ Returns instance data in the form (fqdn, primaryIpAddress, root_password, instance_id) """

    # TODO: naive list access here, make sure to check for duplicates, etc. and bail if necessary

    def vs_lookup():
        _debug and print('Attempting lookup of instance with name: {}; this operation may be repeated on a newly-provisioned system until the OS is installed'.format(name))
        vs_li = client['Account'].getVirtualGuests(mask='mask[id,fullyQualifiedDomainName,hostname,domain,primaryIpAddress,operatingSystem.passwords]')

        return [i for i in vs_li if i and i.get('hostname', '') == name and i.get('domain', '') == domain]

    def root_pass(vs):
        my_vs = vs
        if isinstance(vs, list) and vs:
            my_vs = vs[0]

        _debug and print('my_vs: {}'.format(my_vs))
        if my_vs and 'operatingSystem' in my_vs and 'passwords' in my_vs.get('operatingSystem', {}) and my_vs['operatingSystem']['passwords']:
            passw = [i for i in my_vs['operatingSystem']['passwords'] if i.get('username', '') == 'root']
            if (len(passw) == 1):
                return passw[0]['password']
            elif (len(passw) > 1):
                raise Exception('Multiple "root" passwords found for box {}, bailing'.format(name))
        return None

    matching = vs_lookup()

    if not matching:
        return None
    elif len(matching) == 1:
        # execute vs_lookup for up to <limit> until the first element in the list contains password data
        provisioned = until_with_lim_test(root_pass, 5, 'VM lookup', vs_lookup)[0]
        if provisioned:
            _debug and pprint('queried provisioned box: {}'.format(provisioned))
            return (provisioned['fullyQualifiedDomainName'], provisioned['primaryIpAddress'], root_pass(provisioned), provisioned['id'])
        else:
            raise Exception('failed to locate vs named {} with operatingSystem val'.format(name))
    else:
        raise Exception('Ambiguous results from SL when searching for given hostname: {}. Results: {}'.format(name, matching))

def _report_instance(instance, show_root_pass):
    """ Returns string report for instance; Details intended for easy CLI consumption. Format: <fqdn> <primaryIpAddress> <root_password> <instance_id>"""

    to_print = list(instance)
    if not show_root_pass:
        to_print[2] = 'XXXXX'
    return ' '.join(map(str, to_print))

def _hose_instance(instance):

    _debug and print('hosing instance {}'.format(instance[1]))
    SoftLayer.managers.vs.VSManager(_client).cancel_instance(instance[-1])

def _locate_or_add_pubkey(path_or_label):
    """ If given 'path_or_label' does not pick out a path on the fs, it's assumed this is a label string for a key to look up from SL. If the given label does not pick out an key label in the SL system a RuntimeException is raised.  If the arg does pick out a path on the fs, the file is read and added as a pubkey on the user's account with label set to the key comment in the pubkey file. This key is used in deployment of the new VM. """

    key_manager = SoftLayer.managers.sshkey.SshKeyManager(_client)

    def key_by_label(key_label):
        return [key for key in key_manager.list_keys() if key['label'] == key_label][0]

    if os.path.exists(path_or_label):
        with open(path_or_label, 'r') as keyfile:
            key = keyfile.read()
            # TODO: check that this isn't broken by other supported key formats
            key_label = ''.join(key.split()[-1:])

        # push key to SL
        sl_key = key_by_label(key_label)
        if key_by_label(key_label):
            raise Exception('key read from {} has label "{}" which already exists in SL. Please changed the "comment" field on the provided key, remove the key from SL, or choose a different key for provisioning'.format(path_or_label, key_label))
        else:
            key_manager.add_key(key, key_label)
            return key_by_label(key_label)

    else:
        sl_key = key_by_label(path_or_label)
        if sl_key:
            return sl_key
        else:
            raise Exception('key label specified at invocation, "{}", was not found in SL configured keys. Please provide the label of an existing key or invoke with a new key to-add'.format(path_or_label))

def _ssh_with_retry(instance, fn, lim, *args, **kwargs):
    """ Execute fn with ssh client after connection is established up to max lim minutes of retries. """

    with paramiko.SSHClient() as ssh:
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        def ssh_connect():
            try:
                _debug and print('Attempting to SSH to instance: {}'.format(instance))
                ssh.connect(instance[1], username='root', password=instance[2], look_for_keys=False, allow_agent=False)
                return True
            except Exception as ex:
                _debug and print('failed to SSH to {}, will retry for up to {} min. Original exception: {}'.format(instance[1], ssh_lim_min, ex))
                traceback.print_exc(file=sys.stdout)
                return False

        until_with_lim(lim, 'SSH connect', ssh_connect)
        fn(ssh, *args, **kwargs)

def _upload_salt_seed(ssh, seed_dir):
    """ Uploads seed_dir to instance. Creates temporary tarball in process. """

    tmp_dest_archive = '/tmp/saltmaster_seed.tar.gz'
    with tempfile.TemporaryFile() as tmparchive:
        with tarfile.open(fileobj=tmparchive, mode='w:gz') as tarball:
            tarball.add(seed_dir, arcname='/')
        tmparchive.flush()
        os.fsync(tmparchive.fileno())
        tmparchive.seek(0)

        sftp = ssh.open_sftp()
        sftp.putfo(tmparchive, tmp_dest_archive, confirm=True)

    _print_ssh(ssh.exec_command('ARCHIVE={}; tar xvzf $ARCHIVE -C / && rm -f $ARCHIVE'.format(tmp_dest_archive)))

    _debug and print('uploaded seed dir {} to instance and extracted in /'.format(seed_dir))

def _install_saltmaster_in_docker(ssh):
    """ Installs saltmaster in docker container on instance. """

    # TODO: add success test to system

    _debug and print('installing docker')
    _debug and _print_ssh(ssh.exec_command('yum install -y docker && systemctl enable docker && systemctl start docker'))

    _debug and print('downloading and starting saltmaster container')
    _debug and _print_ssh(ssh.exec_command('mkdir /etc/salt && docker run --name saltmaster -v /etc/salt:/etc/salt -v /srv:/srv -d mdye/v2015_5-saltmaster'))

def _add_sl_cli(ssh):
    """ Installs SL CLI and python lib, copies ~/.softlayer credential file to instance. """

    sftp = ssh.open_sftp()
    sftp.put(os.path.abspath(os.path.join(os.path.expanduser('~'), '.softlayer')), '/root/.softlayer', confirm=True)
    debug and _print_ssh(ssh.exec_command('yum install -y epel-release && yum update && yum install -y python-pip && pip install SoftLayer'))

def main(args):
    """ Entrypoint. Will attempt to read config details, set up an SL client and bootstrap a saltmaster. Accepts parsed CLI args. """

    def my_locate():
        return _locate_instance(args.saltmaster_vm_name, args.saltmaster_vm_domain, _client)

    global _debug
    _debug = True if args.debug else False
    _debug and print('Enabling debug output')

    exit_code = 0

    vs = my_locate()

    if not vs:
        # optionally push key, create a new VM
        if args.ssh_pub_key:
            try:
                ssh_key = _locate_or_add_pubkey(args.ssh_pub_key)
            except Exception as ex:
                _debug and print('Failed to use ssh_key. Original exception: {}'.format(ex))
                sys.exit(1)

        exit_code = 15
        my_vm = copy.deepcopy(_vm_template)
        my_vm.update({'hostname': args.saltmaster_vm_name, 'domain': args.saltmaster_vm_domain, 'sshKeys': [{'id': ssh_key['id']}]})
        _debug and print('creating new server w/ order detail: {}'.format(my_vm))

        try:
            # prefer this to a VSManager in this case b/c createObject blocks
            _client['Virtual_Guest'].createObject(my_vm)
            vs = my_locate()
            if args.seed_dir:
                _ssh_with_retry(vs, _upload_salt_seed, 5, args.seed_dir)
            _ssh_with_retry(vs, _install_saltmaster_in_docker, 5)
            if args.add_sl_cli:
                _ssh_with_retry(vs, _add_sl_cli, 5)

        except Exception as ex:
            print('Failed to create and provision instance named: {}. Hosing it. Original exception: {}'.format(args.saltmaster_vm_name, ex))
            if vs:
                _hose_instance(vs)

    print(_report_instance(vs, args.show_root_pass))
    sys.exit(exit_code)

if __name__ == '__main__':
    argparser = argparse.ArgumentParser()
    argparser.add_argument('saltmaster_vm_name', help='name of SoftLayer VM for existing / desired SaltMaster')
    argparser.add_argument('saltmaster_vm_domain', help='domain of SoftLayer VM for existing / desired SaltMaster')
    argparser.add_argument('--show_root_pass', dest='show_root_pass', action="store_true", help="show root password on stdout")
    argparser.set_defaults(show_root_pass=False)
    argparser.add_argument('--ssh_pub_key', type=str, help="path to public SSH key to add to SoftLayer and this VM's authorized_keys file for root account *or* the name of an existing public SSH key in SL to use for the same purpose")
    argparser.add_argument('--seed_dir', type=str, help="Salt seed dir; should be dir to an fs tree expected to be copied to /")
    argparser.add_argument('--add-sl-cli', dest='add_sl_cli', action="store_true", help="add SL CLI to instance and copy ~/.softlayer credential file")
    argparser.set_defaults(add_sl_cli=False)
    argparser.add_argument('--debug', help='enable debug output', action='store_true')
    main(argparser.parse_args())

# vim: autoindent tabstop=4 shiftwidth=4 expandtab softtabstop=4 filetype=python
