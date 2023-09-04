import ni_mon_client, ni_nfvo_client
import time
import paramiko
import os
import sys
from config import cfg
from server.models.consolidation_info import ConsolidationInfo
import threading

sdn_lullaby_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sdn-lullaby')
sys.path.append(sdn_lullaby_path)

import src.agent.dqn as DQN
import src.agent.ppo as PPO

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

#Configuration
NUMBER_TO_MIGRATE = 3
PROFILE_PERIOD = 10
PROFILE_INTERVAL = 1
record = []

#Data
migrating_vnfs = []
current_consolidating = False
consolidation_list = []

def check_available_resource(node_id):
    node_info = get_node_info()
    selected_node = [ node for node in node_info if node.id == node_id ][-1]
    flavor = ni_mon_api.get_vnf_flavor(cfg["flavor"]["default"])

    if selected_node.n_cores_free >= flavor.n_cores and selected_node.ram_free_mb >= flavor.ram_mb:
        return (selected_node.n_cores_free - flavor.n_cores)

    return False


def check_active_instance(id):
    status = ni_mon_api.get_vnf_instance(id).status

    if status == "ACTIVE":
        return True
    else:
        return False


def get_node_info():
    query = ni_mon_api.get_nodes()

    response = [ node_info for node_info in query if node_info.type == "compute" and node_info.status == "enabled"]
    response = [ node_info for node_info in response if not (node_info.name).startswith("NI-Compute-82-9")]

    return response


def get_node_ip_from_node_id(node_id):
    node_info = get_node_info()

    for info in node_info:
        if info.id == node_id:
            return info.ip

    print("Cannot get node ip from node id")
    return False


def get_node_id_from_node_ip(node_ip):
    node_info = get_node_info()

    for info in node_info:
        if info.ip == node_ip:
            return info.id

    print("Cannot get node id from node ip")
    return False


def get_vnf_info(vnf_id):
    query = ni_mon_api.get_vnf_instance(vnf_id)
    response = query
    
    return response


def get_vnf_ip_from_vnf_id(vnf_id):
    api_response = ni_mon_api.get_vnf_instance(vnf_id)
    ports = api_response.ports
    network_id = openstack_network_id

    for port in ports:
        if port.network_id == network_id:
            return port.ip_addresses[-1]


def get_node_id_from_vnf_id(vnf_id):

    node_id = get_vnf_info(vnf_id).node_id

    return node_id
    

def get_ssh(ssh_ip, ssh_username, ssh_password):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(ssh_ip, username=ssh_username, password=ssh_password)
    return ssh


def get_nfvo_vnf_spec():
#    print("5")

    t = ni_nfvo_client.ApiClient(ni_nfvo_client_cfg)

    ni_nfvo_vnf_spec = ni_nfvo_client.VnfSpec(t)
    ni_nfvo_vnf_spec.flavor_id = cfg["flavor"]["default"]
    ni_nfvo_vnf_spec.user_data = sample_user_data % cfg["instance"]["password"]

    return ni_nfvo_vnf_spec


def set_vnf_spec(vnf_name, node_name):
    vnf_spec = get_nfvo_vnf_spec()
    vnf_spec.vnf_name = vnf_name
    vnf_spec.image_id = cfg["image"]["base"] #client or server
    vnf_spec.node_name = node_name

    return vnf_spec

# Get instance name.
def get_instance_name(vnf_id):

    node_id = get_vnf_info(vnf_id).node_id
    
    node_ip = get_node_ip_from_node_id(node_id)
    host_ssh = get_ssh(node_ip, cfg["openstack_controller"]["username"], cfg["openstack_controller"]["password"])

    command = "LIBVIRT_DEFAULT_URI=qemu:///system virsh list | grep instance | awk '{ print $1 }'"
    stdin, stdout, stderr = host_ssh.exec_command(command)
    
    dom_id = []
    instance_name = ""
    for line in stdout.readlines():
        dom_id.append(line.rstrip())


    for domain in dom_id:
        command = "LIBVIRT_DEFAULT_URI=qemu:///system virsh dominfo " + domain + " | grep -C 5 " + vnf_id + " | grep 'Name' | awk '{ print $2 }'"
        stdin, stdout, stderr = host_ssh.exec_command(command)

        result = stdout.readlines()
        if len(result) > 0:
            instance_name = result[0].rstrip()
            break

    host_ssh.close()
    return instance_name


def connect_target_vnf():
    try:
        get_vnf_info(cfg["migration_test_vm"]["id"])
        print("Success to connect test_vm from config")
    except:
        print("Try to install new vnf for testing migration")
        if check_available_resource(cfg["shared_host"]["host1"]["name"]):
              install_target_vnf(cfg["shared_host"]["host1"]["name"])
        elif check_available_resource(cfg["shared_host"]["host2"]["name"]):
              install_target_vnf(cfg["shared_host"]["host2"]["name"])
        else:
            print("No available resource for installing client or server")

    return True



def ssh_keygen(ue_ssh, ip):
    command = "sudo ssh-keygen -f '/home/ubuntu/.ssh/known_hosts' -R %s" % ip
    stdin, stdout, stderr = ue_ssh.exec_command(command, timeout=120)
    print("ssh-key gen :",stdout.readlines())

    return True



def source_openrc(ctrl_ssh):

    command = "source /opt/stack/devstack/openrc admin demo"
    stdin, stdout, stderr = ctrl_ssh.exec_command(command)

    return


def check_network_topology():
    api_response = ni_mon_api.get_links() # get linked nodes info (node1_id, node2_id)
    print("tt : ", api_response)    
    compute_groups = {}
    for entry in api_response:
        node1_id = entry.node1_id
        node2_id = entry.node2_id

        if "ni-compute" in node2_id :
            if node1_id not in compute_groups:
                compute_groups[node1_id] = []
            compute_groups[node1_id].append(node2_id)

    #'Switch-core-01'
    return compute_groups # 특정 switch에 연결된 compute node들의 정보를 담은 dictionary


def find_related_ni_compute(target_ni_compute, data):
    edge_ni_computes = []

    for switch, ni_compute_list in data.items():
        print(switch, ni_compute_list)
        if target_ni_compute in ni_compute_list:
            edge_ni_computes.extend([ni for ni in ni_compute_list if ni != target_ni_compute])


    data_center_computes = data.get('Switch-core-01', [])

    return edge_ni_computes, data_center_computes





# Call OpenStack live migration API to migrate the target VM to the selected dst host.
def live_migrate(vnf_id, dst_node):

    global migrating_vnfs
    migrating_vnfs.append(vnf_id)

    ctrl_ssh = get_ssh(cfg["openstack_controller"]["ip"], cfg["openstack_controller"]["username"], cfg["openstack_controller"]["password"])
    source_openrc(ctrl_ssh)

    command = "nova live-migration {} {}".format(get_vnf_info(vnf_id).name, dst_node)
    # FIXME: check timeout is needed indeed
    stdin, stdout, stderr = ctrl_ssh.exec_command(command, timeout=120)
    print("migrate readout :",stdout.readlines())

    migrating_vnfs.remove(vnf_id)

    return

def get_busy_vnf_info():

    global migrating_vnfs
    #domjobinfo from all-compute node or otherway to get busy instance
   
    #make list busy_from_backend
    busy_from_backend = []
    
    migrating_vnfs = migrating_vnfs + busy_from_backend
    return migrating_vnfs


def do_learn_consolidation(mode):

    network_info = check_network_topology()
    #ConsolidationInfo(name,flag,model,edge_name,nodes,is_trained)


    for key, value in network_info.items():
        if len(value) == 1 : continue # computing node가 하나밖에 없는 edge는 무시
        response = ConsolidationInfo(key,mode,value,False) # edge 단위로 Consolidation을 생성
        consolidation_list.append(response)
        if mode=="DQN" :
            DQN.start(response)
        else :
            PPO.start(response)
        consolidation_list.remove(response)
    return

def all_consolidation(mode):

    #current_consolidating
    network_info = check_network_topology()

    for key, value in network_info.items():
        if len(value) == 1 : continue
        response = ConsolidationInfo(key,mode,value,True)
        consolidation_list.append(response)
        threading.Thread(target=monitor, args=(mode,response)).start()

    return

def monitor(mode, response):
    while(True):
        if mode=="dqn" :
            DQN.start(response)
        else :
            PPO.start(response)

        if response.get_active_flag() == False :
            consolidation_list.remove(response)
            return
    time.sleep(5)
    return



