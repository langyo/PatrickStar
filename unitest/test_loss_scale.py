# BSD 3-Clause License
#
# Copyright (C) 2021 THL A29 Limited, a Tencent company.  All rights reserved.
#
# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
#
#  * Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
#
#  * Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
#  * Neither the name of the psutil authors nor the names of its contributors
#    may be used to endorse or promote products derived from this software without
#    specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
# ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
# ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import unittest

import torch
from apex import amp
from transformers import BertConfig, BertForSequenceClassification

from common import distributed_test
from data.data_loader import get_bert_data_loader
from patrickstar.runtime import initialize_engine


def bert_model(
    method,
    batch_size=32,
    hidden_dim=768,
    sequence_length=512,
    num_layer=12,
    num_head=12,
    stop_step=10,
):
    # Avoid gpu0 use more memory.
    # https://discuss.pytorch.org/t/extra-10gb-memory-on-gpu-0-in-ddp-tutorial/118113
    rank = torch.distributed.get_rank()
    torch.cuda.empty_cache()

    device = torch.device(f"cuda:{torch.cuda.current_device()}")

    cfg = BertConfig(
        hidden_size=hidden_dim,
        intermediate_size=hidden_dim * 4,
        max_position_embeddings=sequence_length,
        num_attention_heads=num_head,
        num_hidden_layers=num_layer,
    )

    lr = 0.001
    betas = (0.9, 0.999)
    eps = 1e-6
    weight_decay = 0

    # 如果要测试溢出情况的对比，可以将 initial_scale_power 设为 20
    # 但是注意，apex 的 LossScaler 的默认初始值最大为 2**16，所以需要手动在 apex 中修改
    initial_scale_power = 16

    if method == "patrickstar":

        def model_func():
            return BertForSequenceClassification(cfg)

        config = {
            # The same format as optimizer config of DeepSpeed
            # https://www.deepspeed.ai/docs/config-json/#optimizer-parameters
            "optimizer": {
                "type": "Adam",
                "params": {
                    "lr": lr,
                    "betas": betas,
                    "eps": eps,
                    "weight_decay": weight_decay,
                    "use_hybrid_adam": True,
                },
            },
            "fp16": {
                "enabled": True,
                "loss_scale": 0,
                "initial_scale_power": initial_scale_power,
                "loss_scale_window": 1000,
                "hysteresis": 2,
                "min_loss_scale": 1,
            },
            "default_chunk_size": 32 * 1024 * 1024,
            "release_after_init": False,
            "use_cpu_embedding": True,
        }

        model, optimizer = initialize_engine(
            model_func=model_func, local_rank=rank, config=config
        )
    else:
        model = BertForSequenceClassification(cfg)
        model.cuda()
        model.train()
        optimizer = torch.optim.Adam(
            model.parameters(), lr=lr, betas=betas, eps=eps, weight_decay=weight_decay
        )

        if method == "apex":
            model, optimizer = amp.initialize(
                model,
                optimizer,
                opt_level="O2",
                loss_scale="dynamic",
                max_loss_scale=2 ** initial_scale_power,
            )
        else:
            scaler = torch.cuda.amp.GradScaler(
                init_scale=2 ** initial_scale_power,
                growth_factor=2,
                backoff_factor=0.5,
                growth_interval=1000,
            )

        # DDP 不能要求模型部分在cpu部分在gpu
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[rank])

    data_loader = get_bert_data_loader(
        batch_size=batch_size,
        total_samples=10000,
        sequence_length=sequence_length,
        device=device,
        is_distrbuted=True,
    )

    loss_list = []
    scale_list = []
    for n, batch in enumerate(data_loader):
        if n == stop_step:
            break

        optimizer.zero_grad()

        if method == "patrickstar":
            output = model(input_ids=batch[0], labels=batch[1])
            loss = output[0]
            model.backward(loss)
            optimizer.step()
            scale_list.append(optimizer.loss_scaler.loss_scale)
        elif method == "apex":
            output = model(input_ids=batch[0], labels=batch[1])
            loss = output["loss"]
            with amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
            optimizer.step()
            scale_list.append(amp._amp_state.loss_scalers[0]._loss_scale)
        else:
            with torch.cuda.amp.autocast():
                output = model(input_ids=batch[0], labels=batch[1])
            loss = output[0]
            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()
            scale_list.append(scaler.get_scale())

        loss_list.append(loss.item())

        if n == stop_step:
            break

    return loss_list, scale_list


class TestModelInitContext(unittest.TestCase):
    def setUp(self):
        pass

    @distributed_test(world_size=[1], backend="gloo", use_fake_dist=False)
    def test_loss_scale(self):
        # 0.11B
        hidden_dim = 768
        sequence_length = 512
        num_layer = 6
        num_head = 12

        batch_size = 2

        assert hidden_dim % num_head == 0

        # 这里我们采用 torch amp (autocast)，apex O2 和 patrickstar 对比。
        # 其中：
        # torch amp 的策略类似于 apex O1，会更多地使用 fp32，所以其能够适应的 loss scale 可能会更大；
        # apex O2 和 patrickstar 的策略基本相同。
        stop_step = 10
        torch.manual_seed(0)
        torch_res_list, torch_scale_list = bert_model(
            method="torch",
            hidden_dim=hidden_dim,
            batch_size=batch_size,
            sequence_length=sequence_length,
            num_layer=num_layer,
            num_head=num_head,
            stop_step=stop_step,
        )

        torch.cuda.empty_cache()
        print("*" * 50)

        torch.manual_seed(0)
        apex_res_list, apex_scale_list = bert_model(
            method="apex",
            hidden_dim=hidden_dim,
            batch_size=batch_size,
            sequence_length=sequence_length,
            num_layer=num_layer,
            num_head=num_head,
            stop_step=stop_step,
        )

        torch.cuda.empty_cache()
        print("*" * 50)

        torch.manual_seed(0)
        ps_res_list, ps_scale_list = bert_model(
            method="patrickstar",
            hidden_dim=hidden_dim,
            batch_size=batch_size,
            sequence_length=sequence_length,
            num_layer=num_layer,
            num_head=num_head,
            stop_step=stop_step,
        )

        print("loss:")
        print("torch amp:\t", torch_res_list)
        print("apex O2:\t", apex_res_list)
        print("patrickstar:\t", ps_res_list)
        print("")
        print("loss scale:")
        print("torch scale:\t", torch_scale_list)
        print("apex scale:\t", apex_scale_list)
        print("patrickstar:\t", ps_scale_list)


if __name__ == "__main__":
    unittest.main()
