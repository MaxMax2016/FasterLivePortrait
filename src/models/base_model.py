# -*- coding: utf-8 -*-
# @Time    : 2022/12/30 8:57
# @Author  : wenshao
# @Email   : wenshaoguo1026@gmail.com
# @Project : FasterLivePortrait
# @FileName: base_model.py

import copy

from .predictor import get_predictor


class BaseModel:
    """
    模型预测的基类
    """

    def __init__(self, **kwargs):
        self.kwargs = copy.deepcopy(kwargs)
        self.predictor = get_predictor(**self.kwargs)
        if self.predictor is not None:
            self.input_shapes = self.predictor.input_spec()
            self.output_shapes = self.predictor.output_spec()

    def input_process(self, *data):
        """
        输入预处理
        :return:
        """
        pass

    def output_process(self, *data):
        """
        输出后处理
        :return:
        """
        pass

    def predict(self, *data):
        """
        预测
        :return:
        """
        pass

    def __del__(self):
        """
        删除实例
        :return:
        """
        if self.predictor is not None:
            del self.predictor
