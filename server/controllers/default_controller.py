import connexion
import six

from server import util
import threading
import consolidation as cs
from server.models.consolidation_info import ConsolidationInfo

#TODO : DQN, PPO Both consider
def build_test_environment():

    response = cs.setup_env_for_test()

    return response

def get_power_consumption():
    response = cs.get_power_consumption()
    return response

def auto_consolidation():
    cs.status_consolidation = True
    return "success"


def remove_consolidation():
    cs.status_consolidation = False
    return "success"


def do_power_on(node_id):
    print("hi")
    cs.powerup_node(node_id)
    return "success"

def do_power_off(node_id):
    cs.poweroff_node(node_id)
    return "success"


    
#TODO : Seperate Powermanager and consolidation for effeicient management        
threading.Thread(target=cs.monitoring, args=()).start() 
