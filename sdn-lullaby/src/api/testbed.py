from typing import List

import numpy as np

from src.api.api import Api
from src.dataType import Edge, Server, VNF, SFC
import ni_mon_client, ni_nfvo_client
from ni_mon_client.rest import ApiException
from ni_nfvo_client.rest import ApiException
import datetime
import json
import time
import requests
import paramiko
import subprocess
from config import cfg
from multiprocessing.pool import ThreadPool
from server.models.consolidation_info import ConsolidationInfo

# OpenStack Parameters
openstack_network_id = cfg["openstack_network_id"] # Insert OpenStack Network ID to be used for creating SFC
sample_user_data = "#cloud-config\n password: %s\n chpasswd: { expire: False }\n ssh_pwauth: True\n manage_etc_hosts: true\n runcmd:\n - sysctl -w net.ipv4.ip_forward=1"
#ni_nfvo_client_api
ni_nfvo_client_cfg = ni_nfvo_client.Configuration()
ni_nfvo_client_cfg.host=cfg["ni_nfvo"]["host"]
ni_nfvo_vnf_api = ni_nfvo_client.VnfApi(ni_nfvo_client.ApiClient(ni_nfvo_client_cfg))
ni_nfvo_sfc_api = ni_nfvo_client.SfcApi(ni_nfvo_client.ApiClient(ni_nfvo_client_cfg))
ni_nfvo_sfcr_api = ni_nfvo_client.SfcrApi(ni_nfvo_client.ApiClient(ni_nfvo_client_cfg))

#ni_monitoring_api
ni_mon_client_cfg = ni_mon_client.Configuration()
ni_mon_client_cfg.host = cfg["ni_mon"]["host"]
ni_mon_api = ni_mon_client.DefaultApi(ni_mon_client.ApiClient(ni_mon_client_cfg))

# Data
migrating_vnfs = []

def get_all_vnfs_info():
    query = ni_mon_api.get_vnf_instances()
    response = query

    return response


def get_vnf_info(vnf_id):
    query = ni_mon_api.get_vnf_instance(vnf_id)
    response = query

    return response


def get_node_info(node_id):
    query = ni_mon_api.get_node(node_id)
    response = query

    return response


def get_vnf_flavor(flavor_id):
    query = ni_mon_api.get_vnf_flavor(flavor_id)
    response = query

    return response

def get_sfcs():
    query = ni_nfvo_sfc_api.get_sfcs()
    response = query

    return response


def get_ssh(ssh_ip, ssh_username, ssh_password):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(ssh_ip, username=ssh_username, password=ssh_password)
    return ssh


def source_openrc(ctrl_ssh):
    command = "source /opt/stack/devstack/openrc admin demo"
    stdin, stdout, stderr = ctrl_ssh.exec_command(command)

    return


# Call OpenStack live migration API to migrate the target VM to the selected dst host.
def live_migrate(vnf_id, dst_node):
    migrating_vnfs.append(vnf_id)

    ctrl_ssh = get_ssh(cfg["openstack_controller"]["ip"], cfg["openstack_controller"]["username"],
                       cfg["openstack_controller"]["password"])
    source_openrc(ctrl_ssh)

    command = "nova live-migration {} {}".format(get_vnf_info(vnf_id).name, dst_node)
    # FIXME: check timeout is needed indeed
    stdin, stdout, stderr = ctrl_ssh.exec_command(command, timeout=120)
    print("migrate readout :", stdout.readlines())

    migrating_vnfs.remove(vnf_id)

    return


#Get data from openstack

class Testbed(Api):
    edge: Edge
    srvs: List[Server]
    vnfs: List[VNF]
    sfcs: List[SFC]

    def __init__(self,consolidation) -> None:
        self.node_ids = consolidation.nodes
        self.consolidation = consolidation
        self.srv_n = len(self.node_ids)
        

        self.vnfs = []
        self.srvs = []
        self.sfcs = []
        self.sfc_n = -1
        self.max_vnf_num = -1
        self.srv_cpu_cap = -1
        self.srv_mem_cap = -1
        self.edge = None

        vnfs_data = get_all_vnfs_info()
        container_list = ["19d30e9a-2d22-4a44-9a99-924486eca4b5", "7efbe1ea-3fd1-4084-9da3-789ddc2175fe"]
        #self.vnfs_data = [vnf for vnf in vnfs_data if vnf.node_id in self.node_ids]
        self.vnfs_data = [vnf for vnf in vnfs_data if vnf.node_id in self.node_ids and vnf.id not in container_list]
        #Need for filtering vnfs and sfc for provided node_id list
        #filtered_vnfs_data = [vnf for vnf in vnfs_data if vnf['node_id'] in node_ids]
        #should remove container but cannot delete right now
        #19d30e9a-2d22-4a44-9a99-924486eca4b5

        '''
        sfcs_data = get_sfcs()

        filtered_sfcs_data = []
        for sfc in sfcs_data:
            valid_sfc = False
    
            for vnf_instance_ids in sfc.vnf_instance_ids:
                vnf_id = vnf_instance_ids[0]
        
                if any(vnf_id == vnf.id for vnf in vnfs_data):
                    valid_sfc = True
                    break
    
            if valid_sfc:
                filtered_sfcs_data.append(sfc)

        self.sfcs_data = filtered_sfcs_data
        '''
        self.sfcs_data = get_sfcs()
        self.vnf_id_to_index = {vnf.id: index for index, vnf in enumerate(self.vnfs_data)}
        self.srv_id_to_index = {srv: index for index, srv in enumerate(self.node_ids)}
        self.sfc_id_to_index = {sfc.id: index for index, sfc in enumerate(self.sfcs_data)}


        self.reset()

    def reset(self) -> None:
        """Generate random VNFs and put them into servers
        """


        self.vnfs = []
        self.srvs = []
        self.sfcs = []
        self.edge = None


        for item in self.vnfs_data:
            if item.node_id in self.node_ids:
                vnfs_spec=get_vnf_flavor(item.flavor_id)
                sfc_id = -1#None -># set 0 as default
                for sfc_info in self.sfcs_data:                    
                    if [item.id] in sfc_info.vnf_instance_ids:
                        sfc_id = self.sfc_id_to_index[sfc_info.id]
                        break
                    
                vnf = VNF(
                    id=self.vnf_id_to_index[item.id],
                    oid=item.id,
                    cpu_req=vnfs_spec.n_cores,
                    mem_req=vnfs_spec.ram_mb / 1024,
                    sfc_id= sfc_id,  
                    srv_id=self.srv_id_to_index[item.node_id]
                )
                self.vnfs.append(vnf)


        for node_id in self.node_ids:
            vnf_objects = [vnf for vnf in self.vnfs if vnf.srv_id == self.srv_id_to_index[node_id]]
            server_data = get_node_info(node_id)
            if server_data:
                server = Server(
                    id=self.srv_id_to_index[server_data.id],
                    oid=server_data.id,
                    cpu_cap=server_data.n_cores,
                    mem_cap=server_data.ram_mb / 1024,
                    cpu_load=server_data.n_cores - server_data.n_cores_free,
                    mem_load=(server_data.ram_mb - server_data.ram_free_mb) / 1024,
                    vnfs=vnf_objects
                )
                self.srvs.append(server)

        
        for sfc_info in self.sfcs_data:
            vnfs = [vnf for vnf in self.vnfs if vnf.sfc_id == self.sfc_id_to_index[sfc_info.id]]
            sfc = SFC(
                id=self.sfc_id_to_index[sfc_info.id],
                oid=sfc_info.id,
                vnfs=vnfs
            )
            self.sfcs.append(sfc)

        # sfc가 없는 vnf들 처리
        for vnf in self.vnfs:
            if vnf.sfc_id == -1:
                vnf.sfc_id = len(self.sfcs)
                self.sfc_id_to_index[vnf.sfc_id] = len(self.sfcs)
                self.sfcs.append(SFC(
                    id=len(self.sfcs),
                    oid=None,
                    vnfs=[vnf]
                ))


        self.edge = Edge(
            cpu_cap= sum(srvs.cpu_cap for srvs in self.srvs),
            mem_cap= sum(srvs.mem_cap for srvs in self.srvs),
            cpu_load= sum(srvs.cpu_load for srvs in self.srvs),
            mem_load= sum(srvs.mem_load for srvs in self.srvs),
        )
        

        self.sfc_n = len(self.sfcs)
        self.srv_n = len(self.srvs)
        self.max_vnf_num = len(self.vnfs)
        self.srv_cpu_cap = self.edge.cpu_cap
        self.srv_mem_cap = self.edge.mem_cap


    def move_vnf(self, vnf_id: int, srv_id: int) -> bool:


        # vnf_id가 존재하는지 확인
        target_vnf = None
        for srv in self.srvs:
            for vnf in srv.vnfs:
                if vnf.id == vnf_id:
                    target_vnf = vnf
                    break
            if target_vnf is not None:
                break
        if target_vnf is None:
            return False
        # srv_id가 존재하는지 확인
        if srv_id >= len(self.srvs):
            return False
        # 해당 srv에 이미 vnf가 존재하는지 확인
        for vnf in self.srvs[srv_id].vnfs:
            if vnf.id == vnf_id:
                return False
        # capacity 확인
        srv_remain_cpu_cap = self.srvs[srv_id].cpu_cap - self.srvs[srv_id].cpu_load
        srv_remain_mem_cap = self.srvs[srv_id].mem_cap - self.srvs[srv_id].mem_load
        if srv_remain_cpu_cap < target_vnf.cpu_req or srv_remain_mem_cap <target_vnf.mem_req:
            return False
        # vnf 검색 및 이동 (없으면 False 리턴)
        for srv in self.srvs:
            for vnf in srv.vnfs:
                if vnf.id == vnf_id:                 
                    vnf.srv_id = srv_id
                    self.srvs[srv_id].vnfs.append(vnf)
                    self.srvs[srv_id].cpu_load += vnf.cpu_req
                    self.srvs[srv_id].mem_load += vnf.mem_req

                    srv.vnfs.remove(vnf)
                    srv.cpu_load -= vnf.cpu_req
                    srv.mem_load -= vnf.mem_req

                    return True
        return False

    def get_srvs(self) -> List[Server]:
        return self.srvs

    def get_vnfs(self) -> List[VNF]:
        return self.vnfs

    def get_sfcs(self) -> List[List[VNF]]:
        return self.sfcs

    def get_edge(self) -> Edge:
        return self.edge

    def get_consolidation(self) -> ConsolidationInfo:
        return self.consolidation
