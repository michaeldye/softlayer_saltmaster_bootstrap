# softlayer_saltmaster_bootstrap

(TODO)

## Using the bootstrap script

(TODO)

## Getting Started

(TODO: write! Mention need to obtain a SoftLayer account)

### Development setup tool installation

(Directions tried on Mac OS X 10.10.3, Arch Linux x86_64 on 5/9/15, and Ubuntu 15.04)

#### Steps for Linux

Install python, pip and build tooling. If using Arch Linux, execute:

    sudo pacman -Sy python python-pip gcc sed grep

If using Ubuntu, execute:

    apt-get update && apt-get install python python-pip python-dev build-essential

#### Steps for Max OS

0. Install Xcode from the Apple store

0. Accept the gcc license agreement:

        sudo gcc

0. Install pip:

        sudo easy_install pip

#### Common installation steps

0. Install the softlayer_saltmaster_bootstrap module (execute from the root directory of this project):

        sudo pip install -e ./python/

### SoftLayer CLI tool configuration

Execute:

    slcli config setup

