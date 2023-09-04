# coding: utf-8

from __future__ import absolute_import
from datetime import date, datetime  # noqa: F401

from typing import List, Dict  # noqa: F401

from server.models.base_model_ import Model


class ConsolidationInfo(Model):

    def __init__(self, name, model, nodes, is_trained):

        self.name = name
        self.model = model
        self.nodes = nodes
        self.is_trained = is_trained
        self.active_flag = True

    def get_info(self):
        return {
            "name": self.name,
            "active_flag": self.active_flag,
            "model": self.model,
            "nodes": self.nodes,
            "is_trained": self.is_trained
        }

    def get_name(self):
        return self.name

    def set_name(self, name):
        self.name = name

    def get_active_flag(self):
        return self.active_flag

    def set_active_flag(self, active_flag):
        self.active_flag = active_flag

    def get_model(self):
        return self.model

    def set_model(self, model):
        self.model = model

    def get_nodes(self):
        return self.nodes

    def set_nodes(self, nodes):
        self.nodes = nodes

    def get_is_trained(self):
        return self.is_trained

    def set_is_trained(self, is_trained):
        self.is_trained = is_trained
