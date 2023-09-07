import connexion
import six

from server import util
from consolidation import *
from server.models.consolidation_info import ConsolidationInfo


def learn_consolidation(mode="DQN", vnf_num=0):
    do_learn_consolidation(mode, vnf_num)
    return "sucess"


def do_consolidation(mode="DQN"):

    all_consolidation(mode)
    return "sucess"


def get_busy_vnfs():

    vnfs = get_busy_vnf_info()
    return vnfs


def get_all_consolidation():

    response = []

    for process in consolidation_list:
        response.append(process.get_info())

    return response


def get_consolidation(name):

    response = [ process.get_info() for process in consolidation_list if process.name == name]
    return response


def delete_consolidation(name):
    index = -1
    response = []

    for process in consolidation_list:
        if process.name == name:
            index = consolidation_list.index(process)
            break

    if index > -1:
        response = consolidation_list[index].get_info()
        consolidation_list[index].set_active_flag(False)
        consolidation_list.remove(consolidation_list[index])

    return response
