# Copyright (c) OpenMMLab. All rights reserved.

import math
from typing import Any, Dict, Iterable, List, Optional, Tuple

import torch
import torch.nn.functional as F
from torch import nn

import lmdeploy.pytorch.distributed as dist
from lmdeploy.pytorch.distributed import get_world_rank
from lmdeploy.pytorch.model_inputs import StepContext, StepContextManager
from lmdeploy.pytorch.nn import ApplyRotaryEmb, Attention, RMSNorm, RopeType, SiluAndMul, build_rotary_embedding
from lmdeploy.pytorch.nn.linear import build_colwise_linear, build_merged_colwise_linear, build_rowwise_linear
from lmdeploy.pytorch.nn.moe import SoftmaxTopK, build_fused_moe
from lmdeploy.pytorch.nn.rotary_embedding import YarnParameters
from lmdeploy.pytorch.weight_loader.model_weight_loader import load_weight

from .utils.cudagraph import CudaGraphMixin


def yarn_get_mscale(scale=1, mscale=1):
    if scale <= 1:
        return 1.0
    return 0.1 * mscale * math.log(scale) + 1.0


class DeepseekV2BMM(nn.Module):
    """wrapped bmm."""

    def __init__(self, batch: int, in_features: int, out_features: int, dtype: torch.dtype, device: torch.device):
        super().__init__()
        batch = self._update_batch(batch)

        weight = self.create_weight(batch, in_features, out_features, dtype=dtype, device=device)
        weight = torch.nn.Parameter(weight, requires_grad=False)
        self.register_parameter('weight', weight)
        weight.weight_loader = self.weight_loader

        self.batch = batch
        self.in_features = in_features
        self.out_features = out_features
        self.dtype = dtype
        self.device = device

    def _update_batch(self, batch: int):
        """update out features."""
        world_size, _ = get_world_rank()
        batch = batch // world_size
        return batch

    def create_weight(self, batch: int, in_features: int, out_features: int, dtype: torch.dtype, device: torch.device):
        """create weight."""
        return torch.empty((batch, in_features, out_features), dtype=dtype, device=device)

    def weight_loader(self, param: nn.Parameter, weight: torch.Tensor):
        """weight loader."""
        world_size, rank = get_world_rank()
        weight = weight.chunk(world_size, 0)[rank]
        param.data.copy_(weight)

    def forward(self, x: torch.Tensor, output: torch.Tensor):
        """forward."""
        torch.bmm(x.transpose(0, 1), self.weight, out=output.transpose(0, 1))


class DeepseekV2Attention(nn.Module):
    """deepseekv2 attention."""

    def __init__(self, config: Any, dtype: torch.dtype = None, device: torch.device = None):
        super().__init__()
        quantization_config = getattr(config, 'quantization_config', None)
        self.q_lora_rank = config.q_lora_rank
        self.hidden_size = config.hidden_size
        self.num_heads = config.num_attention_heads
        self.qk_rope_head_dim = config.qk_rope_head_dim
        self.kv_lora_rank = config.kv_lora_rank
        self.v_head_dim = config.v_head_dim
        self.qk_nope_head_dim = config.qk_nope_head_dim
        self.q_head_dim = config.qk_nope_head_dim + config.qk_rope_head_dim
        num_replicate_kv_heads = getattr(config, 'num_replicate_key_value_heads', 1)
        num_key_value_heads = getattr(config, 'num_key_value_heads', 1)
        use_flash_mla = getattr(config, 'use_flash_mla', False)

        if self.q_lora_rank is None:
            self.q_proj = build_colwise_linear(
                self.hidden_size,
                self.num_heads * self.q_head_dim,
                bias=False,
                dtype=dtype,
                device=device,
                is_tp=True,
                quant_config=quantization_config,
            )
        else:
            self.q_a_proj = build_colwise_linear(
                self.hidden_size,
                config.q_lora_rank,
                bias=config.attention_bias,
                dtype=dtype,
                device=device,
                is_tp=False,
                quant_config=quantization_config,
            )
            self.q_a_layernorm = RMSNorm(config.q_lora_rank,
                                         1e-6,
                                         quant_config=quantization_config,
                                         dtype=dtype,
                                         device=device)
            self.q_b_proj = build_colwise_linear(
                config.q_lora_rank,
                self.num_heads * self.q_head_dim,
                bias=False,
                dtype=dtype,
                device=device,
                is_tp=True,
                quant_config=quantization_config,
            )

        self.kv_a_proj_with_mqa = build_colwise_linear(
            self.hidden_size,
            config.kv_lora_rank + config.qk_rope_head_dim,
            bias=config.attention_bias,
            dtype=dtype,
            device=device,
            is_tp=False,
            quant_config=quantization_config,
        )
        self.kv_a_layernorm = RMSNorm(config.kv_lora_rank,
                                      1e-6,
                                      quant_config=quantization_config,
                                      dtype=dtype,
                                      device=device)
        self.kc = DeepseekV2BMM(self.num_heads,
                                config.qk_nope_head_dim,
                                config.kv_lora_rank,
                                dtype=dtype,
                                device=device)

        self.apply_rotary_pos_emb = ApplyRotaryEmb()

        self.softmax_scale = self.q_head_dim**(-0.5)

        if config.rope_scaling is not None:
            mscale_all_dim = config.rope_scaling.get('mscale_all_dim', 0)
            scaling_factor = config.rope_scaling['factor']
            if mscale_all_dim:
                mscale = yarn_get_mscale(scaling_factor, mscale_all_dim)
                self.softmax_scale = self.softmax_scale * mscale * mscale

        self.attn_fwd = Attention(self.num_heads,
                                  config.kv_lora_rank + self.qk_rope_head_dim,
                                  scale=self.softmax_scale,
                                  num_kv_heads=num_key_value_heads,
                                  v_head_size=config.kv_lora_rank,
                                  num_replicate_kv_heads=num_replicate_kv_heads,
                                  use_flash_mla=use_flash_mla)

        self.vc = DeepseekV2BMM(self.num_heads, config.kv_lora_rank, self.v_head_dim, dtype=dtype, device=device)
        self.o_proj = build_rowwise_linear(
            self.num_heads * self.v_head_dim,
            self.hidden_size,
            bias=config.attention_bias,
            dtype=dtype,
            device=device,
            is_tp=True,
            quant_config=quantization_config,
        )

    def _q_proj(self, hidden_states, num_heads: int, nope_size: int, pe_size: int):
        """q proj."""
        q_len = hidden_states.size(1)

        query_states = hidden_states.new_empty(q_len, num_heads, nope_size + pe_size)

        if self.q_lora_rank is None:
            q = self.q_proj(hidden_states)
        else:
            q = self.q_b_proj(self.q_a_layernorm(self.q_a_proj(hidden_states)))
        q = q.view(q_len, num_heads, self.q_head_dim)
        # q_pe: (q_len, num_heads, qk_rope_head_dim)
        q_nope, q_pe = torch.split(q, [self.qk_nope_head_dim, self.qk_rope_head_dim], dim=-1)
        # q_nope: (q_len, num_heads, kv_lora_rank)
        q_nope_out = query_states[..., :nope_size]
        self.kc(q_nope, q_nope_out)
        return query_states, q_pe

    def _kv_proj(self, hidden_states, nope_size: int):
        """kv proj."""
        # (q_len, 1, nope_size + pe_size)
        key_states = self.kv_a_proj_with_mqa(hidden_states[0, :, None])
        # (q_len, 1, pe_size)
        k_pe = key_states[..., nope_size:]
        # kv_a_layernorm
        value_states = key_states[..., :nope_size]
        value_states = self.kv_a_layernorm(value_states)
        key_states[..., :nope_size] = value_states
        return key_states, value_states, k_pe

    def _qkv_proj(self, hidden_states: torch.Tensor, num_heads: int):
        """qkv proj."""
        nope_size = self.kv_lora_rank
        pe_size = self.qk_rope_head_dim
        query_states, q_pe = self._q_proj(hidden_states, num_heads, nope_size, pe_size)
        key_states, value_states, k_pe = self._kv_proj(hidden_states, nope_size)

        return query_states, key_states, value_states, q_pe, k_pe

    def forward(
        self,
        hidden_states: torch.Tensor,
        rotary_pos_emb: Tuple[torch.FloatTensor, torch.FloatTensor],
        past_key_value: Optional[Tuple[torch.Tensor]] = None,
        attn_metadata: Any = None,
    ):
        """Rewrite of LlamaAttention.forward."""
        world_size, _ = get_world_rank()
        num_heads = self.num_heads // world_size
        nope_size = self.kv_lora_rank
        q_len = hidden_states.size(1)

        # qkv_proj
        query_states, key_states, value_states, q_pe, k_pe = self._qkv_proj(hidden_states, num_heads=num_heads)

        cos, sin = rotary_pos_emb
        q_pe, k_pe = self.apply_rotary_pos_emb(
            q_pe,
            k_pe,
            cos,
            sin,
            inplace=False,
        )
        query_states[..., nope_size:] = q_pe
        key_states[..., nope_size:] = k_pe

        attn_output = self.attn_fwd(
            query_states,
            key_states,
            value_states,
            past_key_value[0],
            past_key_value[0][..., :nope_size],
            attn_metadata,
            k_scales_zeros=None if len(past_key_value) == 2 else past_key_value[2],
            v_scales_zeros=None if len(past_key_value) == 2 else past_key_value[3],
            inplace=True,
        )
        attn_bmm_out = attn_output.new_empty(q_len, num_heads, self.v_head_dim)

        self.vc(attn_output, attn_bmm_out)
        attn_output = attn_bmm_out.flatten(-2, -1)[None]
        attn_output = self.o_proj(attn_output)

        return attn_output


class MoEGate(nn.Module):
    """Deepseek Gate."""

    def __init__(self, config: Any, dtype: torch.dtype = None, device: torch.device = None):
        super().__init__()
        self.config = config
        self.top_k = config.num_experts_per_tok
        self.n_routed_experts = config.n_routed_experts
        self.routed_scaling_factor = config.routed_scaling_factor
        self.scoring_func = config.scoring_func
        self.alpha = config.aux_loss_alpha
        self.seq_aux = config.seq_aux
        self.topk_method = config.topk_method
        self.n_group = config.n_group
        self.topk_group = config.topk_group
        self.norm_topk_prob = config.norm_topk_prob
        self.renormalize = self.top_k > 1 and self.norm_topk_prob

        # topk selection algorithm
        self.norm_topk_prob = config.norm_topk_prob
        self.gating_dim = config.hidden_size
        self.weight = nn.Parameter(torch.empty((self.n_routed_experts, self.gating_dim), dtype=dtype, device=device))
        if self.topk_method == 'noaux_tc':
            self.e_score_correction_bias = nn.Parameter(
                torch.empty((self.n_routed_experts, ), dtype=dtype, device=device))
        self.softmax_topk = SoftmaxTopK(self.top_k)

    def _compute_scores(self, logits: torch.Tensor):
        """compute scores."""
        if self.scoring_func == 'softmax':
            scores = logits.softmax(dim=-1, dtype=torch.float32)
        elif self.scoring_func == 'sigmoid':
            scores = logits.sigmoid()
        else:
            raise NotImplementedError('insupportable scoring function '
                                      f'for MoE gating: {self.scoring_func}')
        return scores

    def forward(self, hidden_states: torch.Tensor):
        """forward."""
        sequence_length, hidden_dim = hidden_states.shape
        router_logits = F.linear(hidden_states, self.weight)

        if self.topk_method == 'greedy':
            topk_weight, topk_idx = self.softmax_topk(router_logits)
        elif self.topk_method == 'group_limited_greedy':
            scores = router_logits
            grouped_logits = scores.unflatten(-1, (self.n_group, -1))
            group_scores = (grouped_logits.max(-1).values)
            group_idx = torch.topk(group_scores, k=self.topk_group, dim=-1, sorted=False)[1]  # [n, top_k_group]
            group_mask = torch.zeros_like(group_scores)  # [n, n_group]
            group_mask.scatter_(1, group_idx, 1)  # [n, n_group]
            group_mask = ~group_mask.bool()[..., None]
            grouped_logits = grouped_logits.masked_fill(group_mask, 0.0)
            scores = grouped_logits.flatten(1, 2)
            topk_weight, topk_idx = self.softmax_topk(scores)
        elif self.topk_method == 'noaux_tc':
            scores = self._compute_scores(router_logits)
            scores_for_choice = scores.view(sequence_length, -1) + self.e_score_correction_bias[None]
            group_scores = (scores_for_choice.view(sequence_length, self.n_group,
                                                   -1).topk(2, dim=-1)[0].sum(dim=-1))  # [n, n_group]
            group_idx = torch.topk(group_scores, k=self.topk_group, dim=-1, sorted=False)[1]  # [n, top_k_group]
            group_mask = torch.zeros_like(group_scores)  # [n, n_group]
            group_mask.scatter_(1, group_idx, 1)  # [n, n_group]
            score_mask = (group_mask.unsqueeze(-1).expand(sequence_length, self.n_group,
                                                          self.n_routed_experts // self.n_group).reshape(
                                                              sequence_length, -1))  # [n, e]
            tmp_scores = scores_for_choice.masked_fill(~score_mask.bool(), 0.0)  # [n, e]
            _, topk_idx = torch.topk(tmp_scores, k=self.top_k, dim=-1, sorted=False)
            topk_weight = scores.gather(1, topk_idx)
        else:
            raise RuntimeError(f'Unsupported topk_method: {self.topk_method}')

        if self.renormalize:
            denominator = topk_weight.sum(dim=-1, keepdim=True) + 1e-20
            topk_weight = topk_weight / denominator
            if not topk_weight.is_contiguous():
                topk_weight = topk_weight.contiguous()

        if not self.renormalize or self.topk_method == 'noaux_tc':
            topk_weight = topk_weight * self.routed_scaling_factor
        return topk_weight, topk_idx


class DeepseekV2MoE(nn.Module):
    """Deepseek v2 MoE."""

    def __init__(self, config: Any, dtype: torch.dtype = None, device: torch.device = None):
        super().__init__()
        quantization_config = getattr(config, 'quantization_config', None)
        self.hidden_dim = config.hidden_size
        self.ffn_dim = config.moe_intermediate_size
        self.num_experts = config.n_routed_experts
        self.top_k = config.num_experts_per_tok
        self.norm_topk_prob = config.norm_topk_prob
        self.routed_scaling_factor = config.routed_scaling_factor
        self.renormalize = self.top_k > 1 and self.norm_topk_prob
        self.topk_method = config.topk_method
        self.n_group = config.n_group
        self.topk_group = config.topk_group

        self.gate = MoEGate(config, dtype=dtype, device=device)

        self.experts = build_fused_moe(
            self.hidden_dim,
            self.ffn_dim,
            self.num_experts,
            top_k=self.top_k,
            renormalize=False,
            dtype=dtype,
            device=device,
            all_reduce=False,
            quant_config=quantization_config,
        )

        self.shared_experts = None
        if config.n_shared_experts is not None:
            intermediate_size = (config.moe_intermediate_size * config.n_shared_experts)
            self.shared_experts = DeepseekV2MLP(
                config=config,
                intermediate_size=intermediate_size,
                dtype=dtype,
                device=device,
                is_tp=True,
                all_reduce=False,
            )
        world_size, _ = get_world_rank()
        if world_size > 1:
            self._all_reduce = True
        else:
            self._all_reduce = False

    def forward(self, hidden_states: torch.Tensor):
        """forward."""
        batch_size, sequence_length, hidden_dim = hidden_states.shape
        hidden_states = hidden_states.view(-1, hidden_dim)
        topk_weights, topk_ids = self.gate(hidden_states)

        out_states = self.experts(
            hidden_states,
            topk_weights,
            topk_ids,
        )

        if self.shared_experts is not None:
            shared_states = self.shared_experts(hidden_states)
            out_states += shared_states
        out_states = out_states.reshape(batch_size, sequence_length, -1)

        if self._all_reduce:
            dist.all_reduce(out_states)

        return out_states


class DeepseekV2MLP(nn.Module):
    """Deepseek v2 mlp."""

    def __init__(self,
                 config: Any,
                 intermediate_size: int = None,
                 dtype: torch.dtype = None,
                 device: torch.device = None,
                 is_tp: bool = True,
                 all_reduce: bool = True):
        super().__init__()
        quantization_config = getattr(config, 'quantization_config', None)
        # gate up
        if intermediate_size is None:
            intermediate_size = config.intermediate_size
        self.gate_up_proj = build_merged_colwise_linear(
            config.hidden_size,
            [intermediate_size, intermediate_size],
            bias=False,
            dtype=dtype,
            device=device,
            quant_config=quantization_config,
            is_tp=is_tp,
        )

        # silu and mul
        self.act_fn = SiluAndMul(inplace=True)

        # down
        self.down_proj = build_rowwise_linear(
            intermediate_size,
            config.hidden_size,
            bias=False,
            quant_config=quantization_config,
            dtype=dtype,
            device=device,
            is_tp=is_tp,
            all_reduce=all_reduce,
        )

    def forward(self, x):
        """forward."""
        gate_up = self.gate_up_proj(x)
        act = self.act_fn(gate_up)
        return self.down_proj(act)


class DeepseekV2DecoderLayer(nn.Module):
    """Deepseekv2 decoder layer."""

    def __init__(self, config: Any, layer_idx: int, dtype: torch.dtype = None, device: torch.device = None):
        super().__init__()
        self.layer_idx = layer_idx
        quantization_config = None

        # build attention layer
        if getattr(config, 'use_mla', True):
            self.self_attn = DeepseekV2Attention(config, dtype=dtype, device=device)
        else:
            # deepseek-vl2-tiny uses MHA LlamaAttention structure
            from lmdeploy.pytorch.models.llama import LlamaAttention
            self.self_attn = LlamaAttention(config, dtype=dtype, device=device)

        # mlp
        self.mlp = (DeepseekV2MoE(config, dtype=dtype, device=device) if
                    (config.n_routed_experts is not None and layer_idx >= config.first_k_dense_replace
                     and layer_idx % config.moe_layer_freq == 0) else DeepseekV2MLP(config, dtype=dtype, device=device))

        # build input layer norm
        self.input_layernorm = RMSNorm(config.hidden_size,
                                       config.rms_norm_eps,
                                       quant_config=quantization_config,
                                       dtype=dtype,
                                       device=device)

        # build attention layer norm
        self.post_attention_layernorm = RMSNorm(config.hidden_size, config.rms_norm_eps, dtype=dtype, device=device)

    def forward(
        self,
        hidden_states: torch.Tensor,
        rotary_pos_emb: Tuple[torch.FloatTensor, torch.FloatTensor],
        past_key_value: Optional[List[torch.FloatTensor]],
        residual: Optional[torch.Tensor] = None,
        attn_metadata: Any = None,
    ) -> Tuple[torch.FloatTensor, torch.FloatTensor]:

        if residual is None:
            residual = hidden_states
            hidden_states = self.input_layernorm(hidden_states)
        else:
            hidden_states, residual = self.input_layernorm(hidden_states, residual)

        # Self Attention
        hidden_states = self.self_attn(
            hidden_states=hidden_states,
            rotary_pos_emb=rotary_pos_emb,
            past_key_value=past_key_value,
            attn_metadata=attn_metadata,
        )

        # Fully Connected
        hidden_states, residual = self.post_attention_layernorm(hidden_states, residual)
        hidden_states = self.mlp(hidden_states)

        outputs = (hidden_states, residual)
        return outputs


class DeepseekV2Model(nn.Module):
    """mixtral model."""

    def __init__(self, config: Any, dtype: torch.dtype = None, device: torch.device = None):
        super().__init__()
        self.config = config
        self.padding_idx = config.pad_token_id
        self.vocab_size = config.vocab_size
        self.embed_tokens = nn.Embedding(config.vocab_size,
                                         config.hidden_size,
                                         self.padding_idx,
                                         dtype=dtype,
                                         device=device)
        self.layers = nn.ModuleList([
            DeepseekV2DecoderLayer(config, layer_idx, dtype=dtype, device=device)
            for layer_idx in range(config.num_hidden_layers)
        ])

        # build norm
        self.norm = RMSNorm(config.hidden_size, config.rms_norm_eps, quant_config=None, dtype=dtype, device=device)

        emb_type = RopeType.LinearScaling
        rope_dim = config.qk_rope_head_dim if getattr(config, 'use_mla', True) else (config.hidden_size //
                                                                                     config.num_attention_heads)
        rope_max_pos_emb = config.max_position_embeddings
        rope_base = config.rope_theta
        scaling_factor = 1.0
        other_params = dict()
        if config.rope_scaling is not None:
            scaling_type = config.rope_scaling['type']
            scaling_factor = config.rope_scaling['factor']
            if scaling_type == 'dynamic':
                emb_type = RopeType.DynamicNTKScaling
            elif scaling_type == 'yarn':
                emb_type = RopeType.Yarn
                rope_max_pos_emb = config.rope_scaling.get('original_max_position_embeddings', 4096)
                kwargs = {
                    key: config.rope_scaling[key]
                    for key in [
                        'beta_fast',
                        'beta_slow',
                        'mscale',
                        'mscale_all_dim',
                    ] if key in self.config.rope_scaling
                }
                yarn_params = YarnParameters(**kwargs)
                other_params['yarn_params'] = yarn_params
        self.rotary_emb = build_rotary_embedding(rope_dim,
                                                 rope_max_pos_emb,
                                                 rope_base,
                                                 scaling_factor,
                                                 emb_type=emb_type,
                                                 **other_params)

    def forward(
        self,
        input_ids: torch.LongTensor = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        attn_metadata: Any = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
    ):
        """forward."""
        if inputs_embeds is None:
            inputs_embeds = self.embed_tokens(input_ids)

        hidden_states = inputs_embeds
        residual = None
        cos, sin = self.rotary_emb(hidden_states, position_ids)
        cos, sin = cos[0], sin[0]
        rotary_pos_emb = (cos, sin)
        for idx, decoder_layer in enumerate(self.layers):
            past_key_value = past_key_values[idx]
            hidden_states, residual = decoder_layer(
                hidden_states,
                rotary_pos_emb=rotary_pos_emb,
                past_key_value=past_key_value,
                residual=residual,
                attn_metadata=attn_metadata,
            )

        hidden_states, _ = self.norm(hidden_states, residual)

        return hidden_states

    def get_input_embeddings(self):
        """get input embeddings."""
        return self.embed_tokens


class DeepseekV2ForCausalLM(nn.Module, CudaGraphMixin):
    """mixture model for causalLM."""

    def __init__(self,
                 config: Any,
                 ctx_mgr: StepContextManager,
                 dtype: torch.dtype = None,
                 device: torch.device = None):
        super().__init__()
        self.config = config
        self.quantization_config = getattr(config, 'quantization_config', None)
        self.dtype = dtype
        self.ctx_mgr = ctx_mgr
        self.model = DeepseekV2Model(config, dtype=dtype, device=device)
        # build lm_head
        self.lm_head = build_rowwise_linear(config.hidden_size,
                                            config.vocab_size,
                                            bias=False,
                                            dtype=dtype,
                                            device=device)
        self._load_buffers = dict()

    def forward(
        self,
        input_ids: torch.Tensor,
        position_ids: torch.Tensor,
        past_key_values: List[List[torch.Tensor]],
        attn_metadata: Any = None,
        inputs_embeds: torch.Tensor = None,
        **kwargs,
    ):
        hidden_states = self.model(
            input_ids=input_ids,
            position_ids=position_ids,
            past_key_values=past_key_values,
            attn_metadata=attn_metadata,
            inputs_embeds=inputs_embeds,
        )
        return hidden_states

    def get_logits(self, hidden_states: torch.Tensor):
        """compute logits of the model output."""
        return self.lm_head(hidden_states)

    def get_input_embeddings(self):
        """get input embeddings."""
        return self.model.get_input_embeddings()

    def prepare_inputs_for_generation(
        self,
        past_key_values: List[List[torch.Tensor]],
        inputs_embeds: Optional[torch.Tensor] = None,
        context: StepContext = None,
    ):
        """prepare input."""
        input_ids = context.input_ids
        position_ids = context.position_ids
        attn_metadata = context.attn_metadata

        return dict(
            input_ids=input_ids,
            position_ids=position_ids,
            past_key_values=past_key_values,
            attn_metadata=attn_metadata,
            inputs_embeds=inputs_embeds,
        )

    def _load_weight_experts(self, name: str, loaded_weight: torch.Tensor, params_dict: Dict[str, nn.Parameter],
                             expert_params_mapping: List):
        """load weight experts."""
        for (param_name, weight_name, expert_id, shard_id) in expert_params_mapping:
            if weight_name not in name:
                continue
            name = name.replace(weight_name, param_name)
            param = params_dict[name]
            load_weight(param, loaded_weight, expert_id=expert_id, shard_id=shard_id)
            break
        else:
            param = params_dict[name]
            load_weight(param, loaded_weight)

    def _load_weight_attention(self, name: str, loaded_weight: torch.Tensor, params_dict: Dict[str, nn.Parameter],
                               update_pe_mapping: List):
        """load weight attention."""
        device = next(iter(params_dict.values())).device

        def __update_pe(weight, head_dim: int, pe_dim_offset: int):
            # (num_heads, q_head_dim, input_dim)
            weight = weight.unflatten(0, (-1, head_dim))
            # (num_heads, nope_head_dim, input_dim)
            w_pe = weight[:, pe_dim_offset:]
            # (num_heads, nope_head_dim//2, 2, input_dim)
            new_w_pe = w_pe.unflatten(1, (-1, 2))
            # (num_heads, nope_head_dim, input_dim)
            new_w_pe = new_w_pe.transpose(1, 2).flatten(1, 2)
            weight[:, pe_dim_offset:] = new_w_pe
            weight = weight.flatten(0, 1)
            return weight

        def __load_kcvc(name: str, weight: torch.Tensor):
            """load kc and vc from weight."""
            config = self.config
            v_head_dim = config.v_head_dim
            qk_nope_head_dim = config.qk_nope_head_dim
            w_kc, w_vc = weight.unflatten(0, (-1, qk_nope_head_dim + v_head_dim)).split([qk_nope_head_dim, v_head_dim],
                                                                                        dim=1)
            w_vc = w_vc.transpose(1, 2).contiguous()
            kc_param_name = name.replace('.kv_b_proj', '.kc')
            param_kc = params_dict[kc_param_name]
            load_weight(param_kc, w_kc)
            vc_param_name = name.replace('.kv_b_proj', '.vc')
            param_vc = params_dict[vc_param_name]
            load_weight(param_vc, w_vc)

        def __dequant_weight(weight: torch.Tensor, scale: torch.Tensor, dtype: torch.dtype):
            """dequant weight."""
            dim_w0, dim_w1 = weight.shape
            dim_s0, dim_s1 = scale.shape
            assert dim_w0 % dim_s0 == 0
            assert dim_w1 % dim_s1 == 0
            group0 = dim_w0 // dim_s0
            group1 = dim_w1 // dim_s1
            weight = weight.reshape(dim_s0, group0, dim_s1, group1)
            scale = scale.reshape(dim_s0, 1, dim_s1, 1)
            weight = weight.to(scale.dtype) * scale
            weight = weight.to(dtype)
            weight = weight.reshape(dim_w0, dim_w1)
            return weight

        def __load_kcvc_blocked_fp8(name: str, loaded_weight: torch.Tensor):
            """dequant weight."""
            if name.endswith('.weight'):
                weight_name = name
                scale_name = name.replace('.weight', '.scale')
            elif name.endswith('.scale'):
                weight_name = name.replace('.scale', '.weight')
                scale_name = name
            self._load_buffers[name] = loaded_weight
            if (weight_name in self._load_buffers and scale_name in self._load_buffers):
                weight = self._load_buffers.pop(weight_name)
                scale = self._load_buffers.pop(scale_name)
                kc_param_name = weight_name.replace('.kv_b_proj', '.kc')
                dtype = params_dict[kc_param_name].dtype
                weight = __dequant_weight(weight, scale, dtype)
                __load_kcvc(weight_name, weight)

        for (mod_name, head_dim, pe_dim_offset) in update_pe_mapping:
            if mod_name not in name:
                continue
            if name.endswith('.scale'):
                weight = loaded_weight
            else:
                loaded_weight = loaded_weight.to(device)
                weight = __update_pe(loaded_weight, head_dim, pe_dim_offset)
            param = params_dict[name]
            load_weight(param, weight)
            break
        else:
            if '.kv_b_proj' in name:
                quantization_config = self.quantization_config
                quant_method = None
                if quantization_config is not None:
                    quant_method = quantization_config.get('quant_method')

                loaded_weight = loaded_weight.to(device)
                if quant_method == 'fp8':
                    # update blocked fp8 weight
                    __load_kcvc_blocked_fp8(name, loaded_weight)
                else:
                    __load_kcvc(name, loaded_weight)
            else:
                param = params_dict[name]
                load_weight(param, loaded_weight)

    def load_weights(self, weights: Iterable[Tuple[str, torch.Tensor]]):
        """load weights."""

        def __skip_nextn(name, nextn_keys):
            for nextn_key in nextn_keys:
                if nextn_key in name:
                    return True
            return False

        stacked_params_mapping = [
            # (param_name, shard_name, shard_id)
            ('.gate_up_proj', '.gate_proj', 0),
            ('.gate_up_proj', '.up_proj', 1),
        ]

        scale_suffix = '.weight_scale_inv'

        config = self.config

        update_pe_mapping = []
        if getattr(config, 'use_mla', True):
            qk_rope_head_dim = config.qk_rope_head_dim
            kv_lora_rank = config.kv_lora_rank
            qk_nope_head_dim = config.qk_nope_head_dim
            q_head_dim = config.qk_nope_head_dim + config.qk_rope_head_dim
            kv_dim = kv_lora_rank + qk_rope_head_dim
            update_pe_mapping = [('q_proj', q_head_dim, qk_nope_head_dim), ('q_b_proj', q_head_dim, qk_nope_head_dim),
                                 ('kv_a_proj_with_mqa', kv_dim, kv_lora_rank)]
        else:
            # deepseek-vl2-tiny uses MHA LlamaAttention, weight loading differs from MLA
            stacked_params_mapping.extend([
                # (param_name, shard_name, shard_id)
                ('.qkv_proj', '.q_proj', 'q'),
                ('.qkv_proj', '.k_proj', 'k'),
                ('.qkv_proj', '.v_proj', 'v'),
            ])

        num_experts = self.config.n_routed_experts
        expert_params_mapping = []
        for exp_id in range(num_experts):
            gate_param = ('.experts.gate_up', f'.experts.{exp_id}.gate_proj', exp_id, 'gate')
            up_param = ('.experts.gate_up', f'.experts.{exp_id}.up_proj', exp_id, 'up')
            down_param = ('.experts.down', f'.experts.{exp_id}.down_proj', exp_id, 'down')
            expert_params_mapping += [gate_param, up_param, down_param]

        num_hidden_layers = self.config.num_hidden_layers

        num_nextn_predict_layers = getattr(self.config, 'num_nextn_predict_layers', 1)
        nextn_keys = [f'.layers.{num_hidden_layers+i}' for i in range(num_nextn_predict_layers)]

        params_dict = dict(self.named_parameters())
        for name, loaded_weight in weights:
            if 'rotary_emb.inv_freq' in name:
                continue
            if ('rotary_emb.cos_cached' in name or 'rotary_emb.sin_cached' in name):
                continue
            if '.layers' in name:
                # skip nextn
                if __skip_nextn(name, nextn_keys):
                    continue
            if self.config.tie_word_embeddings and 'lm_head.weight' in name:
                continue
            if name.endswith(scale_suffix):
                name = name[:-len(scale_suffix)] + '.scale'
            if '.experts' in name:
                self._load_weight_experts(name, loaded_weight, params_dict, expert_params_mapping=expert_params_mapping)
            elif '.self_attn' in name and getattr(config, 'use_mla', True):
                # attention
                self._load_weight_attention(name, loaded_weight, params_dict, update_pe_mapping)
            else:
                # other
                for (param_name, weight_name, shard_id) in stacked_params_mapping:
                    if weight_name not in name:
                        continue
                    name = name.replace(weight_name, param_name)
                    param = params_dict[name]
                    load_weight(param, loaded_weight, shard_id=shard_id)
                    break
                else:
                    param = params_dict[name]
                    load_weight(param, loaded_weight)
