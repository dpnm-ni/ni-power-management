import ni_mon_client, ni_nfvo_client
import time
import paramiko
import os
import sys
import datetime as dt
from config import cfg
import re
from server.models.consolidation_info import ConsolidationInfo
import threading
import subprocess

sdn_lullaby_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sdn-lullaby')
sys.path.append(sdn_lullaby_path)

import src.agent.dqn as DQN
import src.agent.ppo as PPO

iDRAC = cfg["iDRAC"]

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
status_consolidation = False
consolidation_list = []
power_off_nodes = []


def setup_env_for_test():

    response = "Cannot find test-sfcrs for test"
    deployed_sfcrs = ni_nfvo_sfcr_api.get_sfcrs()

    for sfcr in deployed_sfcrs:
        if sfcr.name.startswith("test-power-management"):
            if get_sfc_by_name(sfcr.name):
                continue
            else:
                print("building environment...")
                response = build_env_for_test(sfcr)  
        else:
            continue
            
    return response


def build_env_for_test(sfcr):


    #Test environment
    target_nodes_0 = ["ni-compute-181-155"]
    target_nodes_1 = ["ni-compute-181-155","ni-compute-181-156","ni-compute-181-156"]
    target_nodes_2 = ["ni-compute-181-157","ni-compute-181-158","ni-compute-181-203"]
    target_nodes_3 = ["ni-compute-181-203","ni-compute-181-203","ni-compute-181-157","ni-compute-181-208","ni-compute-181-158"]
    target_nodes_4 = ["ni-compute-181-158"]

    target_nodes = [target_nodes_0, target_nodes_1, target_nodes_2, target_nodes_3, target_nodes_4]
    
    #Check name is 0, 1, or 2
    idx = int(re.search(r'\d+$', sfcr.name).group())
    
    target_node = target_nodes[idx]
    target_name = sfcr.name + cfg["instance"]["prefix_splitter"]
    target_type = sfcr.nf_chain
    sfc_in_instance_id =[]
    sfc_in_instance = []     
    
    for j in range(0, len(target_type)):
        print("{} {} {}".format(target_type[j], target_node[j], target_name))
        vnf_spec = set_vnf_spec(target_type[j], target_node[j], target_name)
        vnf_id = deploy_vnf(vnf_spec)

        limit = 500 
        for i in range(0, limit):
            time.sleep(2)

            # Success to create VNF instance
            if check_active_instance(vnf_id):
                sfc_in_instance.append([ni_mon_api.get_vnf_instance(vnf_id)])
                sfc_in_instance_id.append([vnf_id])
                break
            elif i == (limit-1):
                print("destroy vnf")
                destroy_vnf(vnf_id)
   
    create_sfc(sfcr, sfc_in_instance_id)
    
    return ("Target sfc : {}".format(sfcr.name))


def create_sfc(sfcr, instance_id_list):

    sfc_spec =ni_nfvo_client.SfcSpec(sfc_name=sfcr.name,
                                 sfcr_ids=[sfcr.id],
                                 vnf_instance_ids=instance_id_list,
                                 is_symmetric=False)


    api_response = ni_nfvo_sfc_api.set_sfc(sfc_spec)
    print("Success to pass for creating sfc")
    return api_response



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


def get_nfvo_vnf_spec2(flavor_name):

    t = ni_nfvo_client.ApiClient(ni_nfvo_client_cfg)

    ni_nfvo_vnf_spec = ni_nfvo_client.VnfSpec(t)
    ni_nfvo_vnf_spec.flavor_id = cfg["flavor"][flavor_name]
    ni_nfvo_vnf_spec.user_data = sample_user_data % cfg["instance"]["password"]

    return ni_nfvo_vnf_spec


def get_nfvo_vnf_spec():
#    print("5")

    t = ni_nfvo_client.ApiClient(ni_nfvo_client_cfg)

    ni_nfvo_vnf_spec = ni_nfvo_client.VnfSpec(t)
    ni_nfvo_vnf_spec.flavor_id = cfg["flavor"]["default"]
    ni_nfvo_vnf_spec.user_data = sample_user_data % cfg["instance"]["password"]

    return ni_nfvo_vnf_spec


def set_vnf_spec(vnf_type, node_name, traffic_name):
    vnf_spec = get_nfvo_vnf_spec2(vnf_type)
    vnf_spec.vnf_name = traffic_name+vnf_type
    vnf_spec.image_id = cfg["image"][vnf_type] #client or server
    vnf_spec.node_name = node_name

    return vnf_spec 

def deploy_vnf(vnf_spec):

    api_response = ni_nfvo_vnf_api.deploy_vnf(vnf_spec)
    print("check deployed")
    print(vnf_spec)
    print(api_response)

    return api_response


def get_sfc_by_name(sfc_name):
#    print("11")

    query = ni_nfvo_sfc_api.get_sfcs()

    sfc_info = [ sfci for sfci in query if sfci.sfc_name == sfc_name ]

    if len(sfc_info) == 0:
        return False

    sfc_info = sfc_info[-1]

    return sfc_info


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


def get_busy_vnf_info():

    global migrating_vnfs
    #domjobinfo from all-compute node or otherway to get busy instance
   
    #make list busy_from_backend
    busy_from_backend = []
    
    migrating_vnfs = migrating_vnfs + busy_from_backend
    return migrating_vnfs


def monitoring():
    global status_consolidation
    global consolidation_list
    global power_off_nodes

    while(True):
    
        #Remove consolidation
        if status_consolidation == False and len(consolidation_list) > 0: 
            for consolidation in consolidation_list:
                consolidation.set_active_flag(False)   
            consolidation_list = []
                
        elif status_consolidation == True and len(consolidation_list) == 0:
            network_info = check_network_topology()
            for key, value in network_info.items():
                if len(value) == 1 : continue
                response = ConsolidationInfo(key,"PPO",value,True)
                consolidation_list.append(response)
                threading.Thread(target=repeat_consolidation, args=(response,)).start()            
                
 
        elif status_consolidation == True and len(consolidation_list) > 0:
            network_info = check_network_topology()
            for key, value in network_info.items():
                if len(value) == 1 : continue 
                for node in value:
                    node_info = ni_mon_api.get_node(node)
                    if node_info.n_cores == node_info.n_cores_free and node_info.id not in power_off_nodes:
                        print("try power off vnf : {}".format(node_info.id))
                        poweroff_node(node_info.id)
                        time.sleep(10)
                        power_off_nodes.append(node_info.id)
                        
                        
        if status_consolidation == False and len(power_off_nodes) > 0 :
            for node in power_off_nodes :
                print("power on for nodes : {}".format(node))
                powerup_node(node)
                time.sleep(10)
            power_off_nodes = []   
                    
        time.sleep(5)

    return

def repeat_consolidation(response):
    while(True):
        PPO.start(response)
        
        if response.get_active_flag() == False :
            return
            
        time.sleep(5)
        
    return

    
def poweroff_node(node):    
    runcommand = 'racadm -r ' + str(iDRAC[node]) + ' -u dpnm -p cjdakfnemfRo serveraction graceshutdown'#if grace shout down is not possible then powerdown
    subprocess.call(runcommand,shell=True)
    return

def powerup_node(node):    
    runcommand = 'racadm -r ' + str(iDRAC[node]) + ' -u dpnm -p cjdakfnemfRo serveraction powerup'
    subprocess.call(runcommand,shell=True)
    return


def get_power_consumption():
    '''
    node_info = get_node_info()
    end_time = dt.datetime.now()
    start_time = end_time - dt.timedelta(seconds = 5)

    if str(end_time)[-1]!='Z':
         end_time = str(end_time.isoformat())+ 'Z'
    if str(start_time)[-1]!='Z':
         start_time = str(start_time.isoformat()) + 'Z'

    resource = "power_consumption___value___gauge"
    total_power_consumption = 0

    for node in node_info:
        query = ni_mon_api.get_measurement(node.id, resource, start_time, end_time)
        print(node.id)
        print(query)
        print(query[0])
        total_power_consumption += query[0].measurement_value
    
    print(total_power_consumption)
    '''

    total_power_consumption = 0
    for index, value in enumerate(iDRAC.values()) : 
        runcommand = 'racadm -r ' + str(value) + ' -u dpnm -p cjdakfnemfRo get System.Power.Realtime.Power | grep -oE \'[0-9]+\' | head -1'

        if index == 2 or index ==  3:
            runcommand = 'racadm -r ' + str(value) + ' -u dpnm -p cjdakfnemfRo getconfig -g cfgServerPower -o cfgServerPowerLastMinAvg | grep -oE \'[0-9]+\' | head -1'
 
        info = int(subprocess.check_output(runcommand,shell=True, encoding='UTF-8').rstrip())
        #info = re.sub(r'[^0-9]',"",info)
        print("{} : {} ".format(list(iDRAC.keys())[index], info))
        total_power_consumption += info

    f = open("test_monitor.txt", "a+", encoding='utf-8')
    f.write(str(total_power_consumption)+'W\n')
    f.close()


    return str(total_power_consumption)+'W'

    


