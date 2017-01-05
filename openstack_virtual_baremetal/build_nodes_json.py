#!/usr/bin/env python
# Copyright 2015 Red Hat Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import argparse
import json
import os
import sys
import yaml

from neutronclient.v2_0 import client as neutronclient
from novaclient import client as novaclient


def _parse_args():
    parser = argparse.ArgumentParser(
        prog='build-nodes-json.py',
        description='Tool for collecting virtual IPMI details',
    )
    parser.add_argument('--env',
                        dest='env',
                        default=None,
                        help='YAML file containing OVB environment details')
    parser.add_argument('--bmc_prefix',
                        dest='bmc_prefix',
                        default='bmc',
                        help='BMC name prefix')
    parser.add_argument('--baremetal_prefix',
                        dest='baremetal_prefix',
                        default='baremetal',
                        help='Baremetal name prefix')
    parser.add_argument('--private_net',
                        dest='private_net',
                        default='private',
                        help='DEPRECATED: This parameter is ignored.')
    parser.add_argument('--provision_net',
                        dest='provision_net',
                        default='provision',
                        help='Provisioning network name')
    parser.add_argument('--nodes_json',
                        dest='nodes_json',
                        default='nodes.json',
                        help='Destination to store the nodes json file to')
    args = parser.parse_args()
    return args


def _get_names(args):
    if args.env is None:
        bmc_base = args.bmc_prefix
        baremetal_base = args.baremetal_prefix
        provision_net = args.provision_net
    else:
        with open(args.env) as f:
            e = yaml.safe_load(f)
        bmc_base = e['parameters']['bmc_prefix']
        baremetal_base = e['parameters']['baremetal_prefix']
        provision_net = e['parameters']['provision_net']
        role = e['parameter_defaults'].get('role')
        if role:
            baremetal_base = baremetal_base[:-len(role) - 1]
    return bmc_base, baremetal_base, provision_net


def _get_clients():
    cloud = os.environ.get('OS_CLOUD')
    if cloud:
        import os_client_config
        nova = os_client_config.make_client('compute', cloud=cloud)
        neutron = os_client_config.make_client('network', cloud=cloud)

    else:
        username = os.environ.get('OS_USERNAME')
        password = os.environ.get('OS_PASSWORD')
        tenant = os.environ.get('OS_TENANT_NAME')
        auth_url = os.environ.get('OS_AUTH_URL')
        if not username or not password or not tenant or not auth_url:
            print('Source an appropriate rc file first')
            sys.exit(1)

        nova = novaclient.Client(2, username, password, tenant, auth_url)
        neutron = neutronclient.Client(
            username=username,
            password=password,
            tenant_name=tenant,
            auth_url=auth_url
        )
    return nova, neutron


def _get_ports(neutron, bmc_base, baremetal_base):
    all_ports = sorted(neutron.list_ports()['ports'], key=lambda x: x['name'])
    bmc_ports = list([p for p in all_ports
                     if p['name'].startswith(bmc_base)])
    bm_ports = list([p for p in all_ports
                    if p['name'].startswith(baremetal_base)])
    if len(bmc_ports) != len(bm_ports):
        raise RuntimeError('Found different numbers of baremetal and '
                           'bmc ports. bmc: %s baremetal: %s' % (bmc_ports,
                                                                 bm_ports))
    return bmc_ports, bm_ports


def _build_nodes(nova, bmc_ports, bm_ports, provision_net, baremetal_base):
    node_template = {
        'pm_type': 'pxe_ipmitool',
        'mac': '',
        'cpu': '',
        'memory': '',
        'disk': '',
        'arch': 'x86_64',
        'pm_user': 'admin',
        'pm_password': 'password',
        'pm_addr': '',
        'capabilities': 'boot_option:local',
        'name': '',
    }

    nodes = []
    bmc_bm_pairs = []
    cache = {}
    for bmc_port, baremetal_port in zip(bmc_ports, bm_ports):
        baremetal = nova.servers.get(baremetal_port['device_id'])
        node = dict(node_template)
        node['pm_addr'] = bmc_port['fixed_ips'][0]['ip_address']
        bmc_bm_pairs.append((node['pm_addr'], baremetal.name))
        node['mac'] = [baremetal.addresses[provision_net][0]['OS-EXT-IPS-MAC:mac_addr']]
        if not cache.get(baremetal.flavor['id']):
            cache[baremetal.flavor['id']] = nova.flavors.get(baremetal.flavor['id'])
        flavor = cache.get(baremetal.flavor['id'])
        node['cpu'] = flavor.vcpus
        node['memory'] = flavor.ram
        node['disk'] = flavor.disk
        # NOTE(bnemec): Older versions of Ironic won't allow _ characters in
        # node names, so translate to the allowed character -
        node['name'] = baremetal.name.replace('_', '-')

        # If a node has uefi firmware ironic needs to be aware of this, in nova
        # this is set using a image property called "hw_firmware_type"
        if not cache.get(baremetal.image['id']):
            cache[baremetal.image['id']] = nova.images.get(baremetal.image['id'])
        image = cache.get(baremetal.image['id'])
        if image.metadata.get('hw_firmware_type') == 'uefi':
            node['capabilities'] += ",boot_mode:uefi"

        bm_name_end = baremetal.name[len(baremetal_base):]
        if '-' in bm_name_end:
            profile = bm_name_end[1:].split('_')[0]
            node['capabilities'] += ',profile:%s' % profile

        nodes.append(node)
    return nodes, bmc_bm_pairs


def _write_nodes(nodes, args):
    with open(args.nodes_json, 'w') as node_file:
        contents = json.dumps({'nodes': nodes}, indent=2)
        node_file.write(contents)
        print(contents)
        print('Wrote node definitions to %s' % args.nodes_json)


# TODO(bnemec): parameterize this based on args.nodes_json
def _write_pairs(bmc_bm_pairs):
    filename = 'bmc_bm_pairs'
    with open(filename, 'w') as pairs_file:
        pairs_file.write('# A list of BMC addresses and the name of the '
                         'instance that BMC manages.\n')
        for i in bmc_bm_pairs:
            pair = '%s %s' % i
            pairs_file.write(pair + '\n')
            print(pair)
        print('Wrote BMC to instance mapping file to %s' % filename)


def main():
    args = _parse_args()
    bmc_base, baremetal_base, provision_net = _get_names(args)
    nova, neutron = _get_clients()
    bmc_ports, bm_ports = _get_ports(neutron, bmc_base, baremetal_base)
    nodes, bmc_bm_pairs = _build_nodes(nova, bmc_ports, bm_ports,
                                       provision_net, baremetal_base)
    _write_nodes(nodes, args)
    _write_pairs(bmc_bm_pairs)


if __name__ == '__main__':
    main()