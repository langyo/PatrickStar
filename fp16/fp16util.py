# Copyright (C) 2021 THL A29 Limited, a Tencent company.
# All rights reserved.
# Licensed under the BSD 3-Clause License (the "License"); you may
# not use this file except in compliance with the License. You may
# obtain a copy of the License at
# https://opensource.org/licenses/BSD-3-Clause
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" basis,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied. See the License for the specific language governing
# permissions and limitations under the License.
# See the AUTHORS file for names of contributors.

# coding=utf-8
# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import torch
import torch.nn as nn
from torch.autograd import Variable
from torch._utils import _flatten_dense_tensors, _unflatten_dense_tensors

from apex.multi_tensor_apply import multi_tensor_applier
import amp_C
from client import HybridPSClient

# from megatron import mpu
import logging
from client import PSTensorStatus


class tofp16(nn.Module):
    """
    Utility module that implements::
        def forward(self, input):
            return input.half()
    """
    def __init__(self):
        super(tofp16, self).__init__()

    def forward(self, input):
        return input.half()


def BN_convert_float(module):
    """
    Utility function for network_to_half().
    Retained for legacy purposes.
    """
    if isinstance(
            module,
            torch.nn.modules.batchnorm._BatchNorm) and module.affine is True:
        module.float()
    for child in module.children():
        BN_convert_float(child)
    return module


def network_to_half(network):
    """
    Convert model to half precision in a batchnorm-safe way.
    Retained for legacy purposes. It is recommended to use FP16Model.
    """
    return nn.Sequential(tofp16(), BN_convert_float(network.half()))


def convert_module(module, dtype):
    """
    Converts a module's immediate parameters and buffers to dtype.
    """
    for param in module.parameters(recurse=False):
        if param is not None:
            if param.data.dtype.is_floating_point:
                param.data = param.data.to(dtype=dtype)
            if param._grad is not None and param._grad.data.dtype.is_floating_point:
                param._grad.data = param._grad.data.to(dtype=dtype)

    for buf in module.buffers(recurse=False):
        if buf is not None and buf.data.dtype.is_floating_point:
            buf.data = buf.data.to(dtype=dtype)


def convert_network(network, dtype):
    """
    Converts a network's parameters and buffers to dtype.
    """
    for module in network.modules():
        if isinstance(module, torch.nn.modules.batchnorm._BatchNorm
                      ) and module.affine is True:
            continue
        convert_module(module, dtype)
    return network


class FP16Model(nn.Module):
    """
    Convert model to half precision in a batchnorm-safe way.
    """
    def __init__(self, network):
        super(FP16Model, self).__init__()
        self.network = convert_network(network, dtype=torch.half)

    def forward(self, *inputs):
        inputs = tuple(t.half() for t in inputs)
        return self.network(*inputs)


def backwards_debug_hook(grad):
    raise RuntimeError(
        "master_params recieved a gradient in the backward pass!")


def prep_param_lists(model, flat_master=False):
    """
    Creates a list of FP32 master parameters for a given model, as in
    `Training Neural Networks with Mixed Precision:  Real Examples`_.
    Args:
        model (torch.nn.Module): Existing Pytorch model
        flat_master (bool, optional, default=False):  Flatten the master parameters into a single tensor, as a performance optimization.
    Returns:
        A tuple (``model_params``, ``master_params``). ``model_params`` is a list of the model's parameters for later use with :func:`model_grads_to_master_grads` and :func:`master_params_to_model_params`.  ``master_params`` is a list of FP32 master gradients.  If ``flat_master=True``, ``master_params`` will be a list with one element.
    Example::
        model_params, master_params = prep_param_lists(model)
    .. warning::
        Currently, if ``flat_master=True``, all the model's parameters must be the same type.  If the model has parameters of different types, use ``flat_master=False``, or use :class:`FP16_Optimizer`.
    .. _`Training Neural Networks with Mixed Precision:  Real Examples`:
        http://on-demand.gputechconf.com/gtc/2018/video/S81012/
    """
    model_params = [
        param for param in model.parameters() if param.requires_grad
    ]

    if flat_master:
        # Give the user some more useful error messages
        try:
            # flatten_dense_tensors returns a contiguous flat array.
            # http://pytorch.org/docs/master/_modules/torch/_utils.html
            master_params = _flatten_dense_tensors(
                [param.data for param in model_params]).float()
        except BaseException:
            print(
                "Error in prep_param_lists:  model may contain a mixture of parameters "
                "of different types.  Use flat_master=False, or use F16_Optimizer."
            )
            raise
        master_params = torch.nn.Parameter(master_params)
        master_params.requires_grad = True
        # master_params.register_hook(backwards_debug_hook)
        if master_params.grad is None:
            master_params.grad = master_params.new(*master_params.size())
        return model_params, [master_params]
    else:
        master_params = [
            param.clone().float().detach() for param in model_params
        ]
        for param in master_params:
            param.requires_grad = True
        return model_params, master_params


def model_grads_to_master_grads(model_params,
                                master_params,
                                flat_master=False,
                                client: HybridPSClient = None):
    """
    Copy model gradients to master gradients.
    Args:
        model_params:  List of model parameters created by :func:`prep_param_lists`.
        master_params:  List of FP32 master parameters created by :func:`prep_param_lists`.  If ``master_params`` was created with ``flat_master=True``, ``flat_master=True`` should also be supplied to :func:`model_grads_to_master_grads`.
    """
    if flat_master:
        if client is not None:
            raise NotImplementedError(
                "not implement flat_master True case in model_grads_to_master_grads"
            )
        # The flattening may incur one more deep copy than is necessary.
        master_params[0].grad.data.copy_(
            _flatten_dense_tensors([p.grad.data for p in model_params]))
    else:
        if client is None:
            for model, master in zip(model_params, master_params):
                if model.grad is not None:
                    if master.grad is None:
                        master.grad = Variable(
                            master.data.new(*master.data.size()))
                else:
                    master.grad = None
            model_grads = [p.grad for p in model_params if p.grad is not None]
            master_grads = [
                p.grad for p in master_params if p.grad is not None
            ]
            _overflow_buf = torch.cuda.IntTensor([0])
            # Fused overflow check + scale for a list of contiguous tensors
            # NOTE(jiaruifang) I found it copys model_grad to master_grad.
            multi_tensor_applier(amp_C.multi_tensor_scale, _overflow_buf,
                                 [model_grads, master_grads], 1.0)
        else:
            for model_p, master_p in zip(model_params, master_params):
                client.access_grad(model_p, torch.device('cuda:0'))
                client.access_grad(master_p, torch.device('cuda:0'))

                model_grad = [model_p.grad]
                master_grad = [master_p.grad]
                _overflow_buf = torch.cuda.IntTensor([0])
                # Fused overflow check + scale for a list of contiguous tensors
                # TODO(jiaruifang) I found it copys model_grad to master_grad.
                multi_tensor_applier(amp_C.multi_tensor_scale, _overflow_buf,
                                     [model_grad, master_grad], 1.0)

                client.release_grad(model_p, PSTensorStatus.FREE)
                client.release_grad(master_p, PSTensorStatus.HOLD)


def master_params_to_model_params(model_params,
                                  master_params,
                                  flat_master=False,
                                  client=None):
    """
    Copy master parameters to model parameters.
    Args:
        model_params:  List of model parameters created by :func:`prep_param_lists`.
        master_params:  List of FP32 master parameters created by :func:`prep_param_lists`.  If ``master_params`` was created with ``flat_master=True``, ``flat_master=True`` should also be supplied to :func:`master_params_to_model_params`.
    """
    if flat_master:
        raise NotImplementedError(
            "master_params_to_model_params flatten is not implemented for HybridPS"
        )
        for model, master in zip(
                model_params,
                _unflatten_dense_tensors(master_params[0].data, model_params)):
            model.data.copy_(master)
    else:
        for model, master in zip(model_params, master_params):
            # TODO(jiaruing) 简单弄成计算设备在cuda上，可以根据model和master现在
            # 所在的设备选择计算设备
            # TODO(jiaruifang) 这个过程可以和FWD计算重叠。
            if client is not None:
                client.access_data(model, torch.device('cuda:0'))
                client.access_data(master, torch.device('cuda:0'))

            model.data.copy_(master.data)

            if client is not None:
                # FP16 param data被标记成hold
                client.release_data(model, PSTensorStatus.HOLD)
                client.release_data(master, PSTensorStatus.HOLD)


# Backward compatibility fixes


def to_python_float(t):
    if hasattr(t, 'item'):
        return t.item()
    else:
        return t[0]


TORCH_MAJOR = int(torch.__version__.split('.')[0])
TORCH_MINOR = int(torch.__version__.split('.')[1])

# clip_grad_norm = mpu.clip_grad_norm
if TORCH_MAJOR == 0 and TORCH_MINOR <= 4:
    clip_grad_norm = torch.nn.utils.clip_grad_norm
else:
    clip_grad_norm = torch.nn.utils.clip_grad_norm_