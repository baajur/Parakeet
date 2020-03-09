# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
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
import math
import paddle.fluid.dygraph as dg
import paddle.fluid as fluid
from parakeet.models.transformer_tts.utils import *
from parakeet.modules.multihead_attention import MultiheadAttention
from parakeet.modules.ffn import PositionwiseFeedForward
from parakeet.models.transformer_tts.prenet import PreNet
from parakeet.models.transformer_tts.post_convnet import PostConvNet


class Decoder(dg.Layer):
    def __init__(self, num_hidden, config, num_head=4, n_layers=3):
        super(Decoder, self).__init__()
        self.num_hidden = num_hidden
        self.num_head = num_head
        param = fluid.ParamAttr()
        self.alpha = self.create_parameter(
            shape=(1, ),
            attr=param,
            dtype='float32',
            default_initializer=fluid.initializer.ConstantInitializer(
                value=1.0))
        self.pos_inp = get_sinusoid_encoding_table(
            1024, self.num_hidden, padding_idx=0)
        self.pos_emb = dg.Embedding(
            size=[1024, num_hidden],
            padding_idx=0,
            param_attr=fluid.ParamAttr(
                initializer=fluid.initializer.NumpyArrayInitializer(
                    self.pos_inp),
                trainable=False))
        self.decoder_prenet = PreNet(
            input_size=config['audio']['num_mels'],
            hidden_size=num_hidden * 2,
            output_size=num_hidden,
            dropout_rate=0.2)
        k = math.sqrt(1 / num_hidden)
        self.linear = dg.Linear(
            num_hidden,
            num_hidden,
            param_attr=fluid.ParamAttr(
                initializer=fluid.initializer.XavierInitializer()),
            bias_attr=fluid.ParamAttr(initializer=fluid.initializer.Uniform(
                low=-k, high=k)))

        self.selfattn_layers = [
            MultiheadAttention(num_hidden, num_hidden // num_head,
                               num_hidden // num_head) for _ in range(n_layers)
        ]
        for i, layer in enumerate(self.selfattn_layers):
            self.add_sublayer("self_attn_{}".format(i), layer)
        self.attn_layers = [
            MultiheadAttention(num_hidden, num_hidden // num_head,
                               num_hidden // num_head) for _ in range(n_layers)
        ]
        for i, layer in enumerate(self.attn_layers):
            self.add_sublayer("attn_{}".format(i), layer)
        self.ffns = [
            PositionwiseFeedForward(
                num_hidden, num_hidden * num_head, filter_size=1)
            for _ in range(n_layers)
        ]
        for i, layer in enumerate(self.ffns):
            self.add_sublayer("ffns_{}".format(i), layer)
        self.mel_linear = dg.Linear(
            num_hidden,
            config['audio']['num_mels'] * config['audio']['outputs_per_step'],
            param_attr=fluid.ParamAttr(
                initializer=fluid.initializer.XavierInitializer()),
            bias_attr=fluid.ParamAttr(initializer=fluid.initializer.Uniform(
                low=-k, high=k)))
        self.stop_linear = dg.Linear(
            num_hidden,
            1,
            param_attr=fluid.ParamAttr(
                initializer=fluid.initializer.XavierInitializer()),
            bias_attr=fluid.ParamAttr(initializer=fluid.initializer.Uniform(
                low=-k, high=k)))

        self.postconvnet = PostConvNet(
            config['audio']['num_mels'],
            config['hidden_size'],
            filter_size=5,
            padding=4,
            num_conv=5,
            outputs_per_step=config['audio']['outputs_per_step'],
            use_cudnn=True)

    def forward(self,
                key,
                value,
                query,
                positional,
                mask,
                m_mask=None,
                m_self_mask=None,
                zero_mask=None):
        """
        Decoder layer of TransformerTTS.
        Args:
            key (Variable): The input key of decoder.
                Shape: (B, T_text, C), T_text means the timesteps of input text,
                dtype: float32. 
            value (Variable): The . input value of decoder.
                Shape: (B, T_text, C), dtype: float32.
            query (Variable): The input query of decoder.
                Shape: (B, T_mel, C), T_mel means the timesteps of input spectrum,
                dtype: float32.
            positional (Variable): The spectrum position. 
                Shape: (B, T_mel), dtype: int64.
            mask (Variable): the mask of decoder self attention.
                Shape: (B, T_mel, T_mel), dtype: int64.
            m_mask (Variable, optional): the query mask of encoder-decoder attention. Defaults to None.
                Shape: (B, T_mel, 1), dtype: int64.
            m_self_mask (Variable, optional): the query mask of decoder self attention. Defaults to None.
                Shape: (B, T_mel, 1), dtype: int64.
            zero_mask (Variable, optional): query mask of encoder-decoder attention. Defaults to None.
                Shape: (B, T_mel, T_text), dtype: int64.
                
        Returns:
            mel_out (Variable): the decoder output after mel linear projection.
                Shape: (B, T_mel, C).
            out (Variable): the decoder output after post mel network.
                Shape: (B, T_mel, C).
            stop_tokens (Variable): the stop tokens of output.
                Shape: (B, T_mel, 1)
            attn_list (list[Variable]): the encoder-decoder attention list.
                Len: n_layers.
            selfattn_list (list[Variable]): the decoder self attention list.
                Len: n_layers.
        """

        # get decoder mask with triangular matrix

        if fluid.framework._dygraph_tracer()._train_mode:
            m_mask = layers.expand(m_mask, [self.num_head, 1, key.shape[1]])
            m_self_mask = layers.expand(m_self_mask,
                                        [self.num_head, 1, query.shape[1]])
            mask = layers.expand(mask, [self.num_head, 1, 1])
            zero_mask = layers.expand(zero_mask, [self.num_head, 1, 1])

        else:
            m_mask, m_self_mask, zero_mask = None, None, None

        # Decoder pre-network
        query = self.decoder_prenet(query)

        # Centered position
        query = self.linear(query)

        # Get position embedding
        positional = self.pos_emb(positional)
        query = positional * self.alpha + query

        #positional dropout
        query = fluid.layers.dropout(
            query, 0.1, dropout_implementation='upscale_in_train')

        # Attention decoder-decoder, encoder-decoder
        selfattn_list = list()
        attn_list = list()

        for selfattn, attn, ffn in zip(self.selfattn_layers, self.attn_layers,
                                       self.ffns):
            query, attn_dec = selfattn(
                query, query, query, mask=mask, query_mask=m_self_mask)
            query, attn_dot = attn(
                key, value, query, mask=zero_mask, query_mask=m_mask)
            query = ffn(query)
            selfattn_list.append(attn_dec)
            attn_list.append(attn_dot)

        # Mel linear projection
        mel_out = self.mel_linear(query)
        # Post Mel Network
        out = self.postconvnet(mel_out)
        out = mel_out + out

        # Stop tokens
        stop_tokens = self.stop_linear(query)
        stop_tokens = layers.squeeze(stop_tokens, [-1])
        stop_tokens = layers.sigmoid(stop_tokens)

        return mel_out, out, attn_list, stop_tokens, selfattn_list
