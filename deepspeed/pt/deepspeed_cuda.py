from torch import nn
from torch.autograd import Function
import torch
import json
import math
import deepspeed_transformer_cuda as ds_transformer_cuda
import deepspeed_stochastic_transformer_cuda as ds_stochastic_transformer_cuda


class TransformerConfig():
    def __init__(self,
                 batch_size,
                 max_seq_length,
                 hidden_size,
                 heads,
                 attn_dropout_ratio,
                 hidden_dropout_ratio,
                 num_hidden_layers,
                 initializer_range):
        self.layer_id = -1
        self.batch_size = batch_size
        self.hidden_size = hidden_size
        self.max_seq_length = max_seq_length
        self.heads = heads
        self.attn_dropout_ratio = attn_dropout_ratio
        self.hidden_dropout_ratio = hidden_dropout_ratio
        self.num_hidden_layers = num_hidden_layers
        self.initializer_range = initializer_range


class DeepSpeedTransformerConfig(TransformerConfig):
    """Initialize the DeepSpeed Transformer Config.

        Arguments:
            batch_size: The maximum batch size used for running the kernel on each GPU

            max_seq_length: The sequence-length of the model being trained with DeepSpeed

            hidden_size: The hidden size of the transformer layer

            heads: The number of heads in the self-attention of the transformer layer

            attn_dropout_ratio: The ratio of dropout for the attention's output

            hidden_dropout_ratio: The ratio of dropout for the transformer's output

            num_hidden_layers: The number of transformer layers

            initializer_range: BERT model's initializer range for initializing parameter data

            local_rank: Optional: The rank of GPU running the transformer kernel, it is not required
                to use if the model already set the current device, otherwise need to set it
                so that the transformer kernel can work on the right device

            seed: The random seed for the dropout layers

            fp16: Enable half-precision computation

            pre_layer_norm: Select between Pre-LN or Post-LN transformer architecture

            normalize_invertible: Optional: Enable invertible LayerNorm execution (dropping the input activation),
                default is False

            gelu_checkpoint: Optional: Enable checkpointing of Gelu activation output to save memory,
                default is False

            adjust_init_range: Optional: Set as True (default) if the model adjusts the weight initial values of
                its self-attention output and layer output, False keeps the initializer_range no change.
                See the adjustment below:
                    output_std = self.config.initializer_range / math.sqrt(2.0 * num_layers)

            attn_dropout_checkpoint: Optional: Enable checkpointing of attention dropout to save memory,
                default is False

            stochastic_mode:  Enable for high performance, please note that this flag has some level of
                non-determinism and can produce different results on different runs.  However, we have seen
                that by enabling it, the pretraining tasks such as BERT are not affected and can obtain
                a high accuracy level. On the other hand, for the downstream tasks, such as fine-tuning, we recommend
                to turn it off in order to be able to reproduce the same result through the regular kernel execution.

            huggingface: Enbale if using the HuggingFace interface style for sending out the forward results.

    """
    def __init__(self,
                 batch_size=-1,
                 max_seq_length=-1,
                 hidden_size=-1,
                 heads=-1,
                 attn_dropout_ratio=-1,
                 hidden_dropout_ratio=-1,
                 num_hidden_layers=-1,
                 initializer_range=-1,
                 local_rank=-1,
                 seed=-1,
                 fp16=False,
                 pre_layer_norm=True,
                 normalize_invertible=False,
                 gelu_checkpoint=False,
                 adjust_init_range=True,
                 attn_dropout_checkpoint=False,
                 stochastic_mode=False,
                 huggingface=False):
        super(DeepSpeedTransformerConfig,
              self).__init__(batch_size,
                             max_seq_length,
                             hidden_size,
                             heads,
                             attn_dropout_ratio,
                             hidden_dropout_ratio,
                             num_hidden_layers,
                             initializer_range)
        self.fp16 = fp16
        self.pre_layer_norm = pre_layer_norm
        self.local_rank = local_rank
        self.seed = seed
        self.normalize_invertible = normalize_invertible
        self.gelu_checkpoint = gelu_checkpoint  # True: if higher batch size is required
        self.adjust_init_range = adjust_init_range
        self.test_gemm = False
        self.training = True
        self.is_grad_enabled = True
        self.attn_dropout_checkpoint = attn_dropout_checkpoint
        self.stochastic_mode = stochastic_mode
        self.huggingface = huggingface

    @classmethod
    def from_dict(cls, json_object):
        config = DeepSpeedTransformerConfig()
        for key, value in json_object.items():
            config.__dict__[key] = value
        return config

    @classmethod
    def from_json_file(cls, json_file):
        with open(json_file, "r", encoding='utf-8') as reader:
            text = reader.read()
        return cls.from_dict(json.loads(text))


class DeepSpeedTransformerFunction(Function):
    @staticmethod
    def forward(ctx,
                input,
                input_mask,
                self,
                grads,
                layer_id,
                attn_qkvw,
                attn_qkvb,
                attn_ow,
                attn_ob,
                attn_nw,
                attn_nb,
                inter_w,
                inter_b,
                output_w,
                output_b,
                norm_w,
                norm_b,
                config):

        bsz = input.shape[0]

        if bsz > config.batch_size:
            raise ValueError('Input batch size exceeds the limit.')

        cuda_module = ds_stochastic_transformer_cuda if config.stochastic_mode else ds_transformer_cuda
        forward_func = cuda_module.forward_fp16 if config.fp16 else cuda_module.forward_fp32

        (output,
         inp_norm,
         qkv_tf,
         soft_inp,
         ctx_bufB,
         attn_o_inp,
         add_res,
         ff1_inp,
         gelu_inp,
         ff2_inp,
         attn_prob_dropout_mask,
         attn_output_dropout_mask,
         layer_output_dropout_mask) = forward_func(config.layer_id,
                                                   input,
                                                   input_mask,
                                                   attn_qkvw,
                                                   attn_qkvb,
                                                   attn_ow,
                                                   attn_ob,
                                                   attn_nw,
                                                   attn_nb,
                                                   inter_w,
                                                   inter_b,
                                                   output_w,
                                                   output_b,
                                                   norm_w,
                                                   norm_b,
                                                   config.training,
                                                   config.pre_layer_norm,
                                                   config.attn_dropout_checkpoint,
                                                   config.normalize_invertible,
                                                   config.gelu_checkpoint)

        # For testing only.
        if grads is not None:
            for i in [2]:
                attn_qkvw.register_hook(
                    lambda x,
                    i=i,
                    self=self: grads.append([
                        x[i * attn_ow.size(0):(i + 1) * attn_ow.size(0)],
                        ("Q_W" if i == 0 else "K_W" if i == 1 else "V_W")
                    ]))
            for i in [2]:
                attn_qkvb.register_hook(
                    lambda x,
                    i=i,
                    self=self: grads.append([
                        x[i * attn_ow.size(0):(i + 1) * attn_ow.size(0)],
                        ("Q_B" if i == 0 else "K_B" if i == 1 else "V_B")
                    ]))

            attn_ow.register_hook(lambda x, self=self: grads.append([x, "O_W"]))
            attn_ob.register_hook(lambda x, self=self: grads.append([x, "O_B"]))
            attn_nw.register_hook(lambda x, self=self: grads.append([x, "N2_W"]))
            attn_nb.register_hook(lambda x, self=self: grads.append([x, "N2_B"]))
            inter_w.register_hook(lambda x, self=self: grads.append([x, "int_W"]))
            inter_b.register_hook(lambda x, self=self: grads.append([x, "int_B"]))
            output_w.register_hook(lambda x, self=self: grads.append([x, "out_W"]))
            output_b.register_hook(lambda x, self=self: grads.append([x, "out_B"]))
            norm_w.register_hook(lambda x, self=self: grads.append([x, "norm_W"]))
            norm_b.register_hook(lambda x, self=self: grads.append([x, "norm_B"]))

        if config.is_grad_enabled:
            if (config.pre_layer_norm and config.normalize_invertible):
                ctx.save_for_backward(input_mask,
                                      attn_qkvw,
                                      attn_qkvb,
                                      attn_ow,
                                      attn_ob,
                                      attn_nw,
                                      attn_nb,
                                      inter_w,
                                      inter_b,
                                      output_w,
                                      output_b,
                                      norm_w,
                                      norm_b)
            else:
                ctx.save_for_backward(output,
                                      input,
                                      input_mask,
                                      attn_qkvw,
                                      attn_qkvb,
                                      attn_ow,
                                      attn_ob,
                                      attn_nw,
                                      attn_nb,
                                      inter_w,
                                      inter_b,
                                      output_w,
                                      output_b,
                                      norm_w,
                                      norm_b)

            ctx.config = config
            if (config.pre_layer_norm or not config.normalize_invertible):
                ctx.inp_norm = inp_norm

            ctx.qkv_tf = qkv_tf
            ctx.soft_inp = soft_inp
            if not config.attn_dropout_checkpoint:
                ctx.ctx_bufB = ctx_bufB

            ctx.attn_o_inp = attn_o_inp
            if not config.normalize_invertible:
                ctx.add_res = add_res

            ctx.ff1_inp = ff1_inp
            if not config.gelu_checkpoint:
                ctx.gelu_inp = gelu_inp

            ctx.ff2_inp = ff2_inp
            ctx.attn_prob_dropout_mask = attn_prob_dropout_mask
            ctx.attn_output_dropout_mask = attn_output_dropout_mask
            ctx.layer_output_dropout_mask = layer_output_dropout_mask

        if config.huggingface:
            return (output, )  # outputs -> (output) : outputs[0] = output
        else:
            return output

    @staticmethod
    def backward(ctx, grad_output):
        bsz = grad_output.shape[0]

        if bsz > ctx.config.batch_size:
            raise ValueError('grad_output batch size exceeds the limit.')

        assert ctx.config.training

        if (ctx.config.pre_layer_norm and ctx.config.normalize_invertible):
            (input_mask,
             attn_qkvw,
             attn_qkvb,
             attn_ow,
             attn_ob,
             attn_nw,
             attn_nb,
             inter_w,
             inter_b,
             output_w,
             output_b,
             norm_w,
             norm_b) = ctx.saved_tensors
        else:
            (output,
             input,
             input_mask,
             attn_qkvw,
             attn_qkvb,
             attn_ow,
             attn_ob,
             attn_nw,
             attn_nb,
             inter_w,
             inter_b,
             output_w,
             output_b,
             norm_w,
             norm_b) = ctx.saved_tensors

        cuda_module = ds_stochastic_transformer_cuda if ctx.config.stochastic_mode else ds_transformer_cuda
        backward_func = cuda_module.backward_fp16 if ctx.config.fp16 else cuda_module.backward_fp32

        (grad_input,
         grad_attn_qkvw,
         grad_attn_qkvb,
         grad_attn_ow,
         grad_attn_ob,
         grad_attn_nw,
         grad_attn_nb,
         grad_inter_w,
         grad_inter_b,
         grad_output_w,
         grad_output_b,
         grad_norm_w,
         grad_norm_b) = backward_func(
             ctx.config.layer_id,
             grad_output,
             (ctx.inp_norm if (ctx.config.pre_layer_norm
                               and ctx.config.normalize_invertible) else output),
             (ctx.inp_norm if (ctx.config.pre_layer_norm
                               or not ctx.config.normalize_invertible) else input),
             ctx.qkv_tf,
             ctx.soft_inp,
             (ctx.soft_inp if ctx.config.attn_dropout_checkpoint else ctx.ctx_bufB),
             ctx.attn_o_inp,
             (ctx.ff1_inp if ctx.config.normalize_invertible else ctx.add_res),
             ctx.ff1_inp,
             (ctx.ff2_inp if ctx.config.gelu_checkpoint else ctx.gelu_inp),
             ctx.ff2_inp,
             ctx.attn_prob_dropout_mask,
             ctx.attn_output_dropout_mask,
             ctx.layer_output_dropout_mask,
             (ctx.inp_norm if (ctx.config.pre_layer_norm
                               and ctx.config.normalize_invertible) else input),
             input_mask,
             attn_qkvw,
             attn_qkvb,
             attn_ow,
             attn_ob,
             attn_nw,
             attn_nb,
             inter_w,
             inter_b,
             output_w,
             output_b,
             norm_w,
             norm_b)

        return (grad_input,
                None,
                None,
                None,
                None,
                grad_attn_qkvw,
                grad_attn_qkvb,
                grad_attn_ow,
                grad_attn_ob,
                grad_attn_nw,
                grad_attn_nb,
                grad_inter_w,
                grad_inter_b,
                grad_output_w,
                grad_output_b,
                grad_norm_w,
                grad_norm_b,
                None)


class DeepSpeedTransformerLayer(nn.Module):
    """Initialize the DeepSpeed Transformer Layer.

        Static variable:
            layer_id: The layer-index counter starting from 0 and incrementing by 1 every time a layer object is instantiated,
            e.g. if a model has 24 transformer layers, layer_id goes from 0 to 23.
        Arguments:
            config: An object of DeepSpeedTransformerConfig

            initial_weights: Optional: Only used for unit test

            initial_biases: Optional: Only used for unit test
    """
    layer_id = 0

    def __init__(self, config, initial_weights=None, initial_biases=None):
        super(DeepSpeedTransformerLayer, self).__init__()

        self.config = config
        self.config.layer_id = DeepSpeedTransformerLayer.layer_id
        DeepSpeedTransformerLayer.layer_id = DeepSpeedTransformerLayer.layer_id + 1

        print("DeepSpeed Transformer config is ", self.config.__dict__)

        if self.config.local_rank >= 0:
            torch.cuda.set_device(self.config.local_rank)

        if initial_weights is None and initial_biases is None:
            self.attn_qkvw = nn.Parameter(
                torch.Tensor(self.config.hidden_size * 3,
                             self.config.hidden_size))
            self.attn_qkvb = nn.Parameter(torch.Tensor(self.config.hidden_size * 3))
            self.attn_ow = nn.Parameter(
                torch.Tensor(self.config.hidden_size,
                             self.config.hidden_size))
            self.attn_ob = nn.Parameter(torch.Tensor(self.config.hidden_size))
            self.attn_nw = nn.Parameter(torch.Tensor(self.config.hidden_size))
            self.attn_nb = nn.Parameter(torch.Tensor(self.config.hidden_size))
            self.inter_w = nn.Parameter(
                torch.Tensor(4 * self.config.hidden_size,
                             self.config.hidden_size))
            self.inter_b = nn.Parameter(torch.Tensor(4 * self.config.hidden_size))
            self.output_w = nn.Parameter(
                torch.Tensor(self.config.hidden_size,
                             4 * self.config.hidden_size))
            self.output_b = nn.Parameter(torch.Tensor(self.config.hidden_size))
            self.norm_w = nn.Parameter(torch.Tensor(self.config.hidden_size))
            self.norm_b = nn.Parameter(torch.Tensor(self.config.hidden_size))
            self.init_transformer_weights(self.config.adjust_init_range)
        else:
            # For testing only.
            self.attn_qkvw = nn.Parameter(
                torch.Tensor(self.config.hidden_size * 3,
                             self.config.hidden_size))
            for i in range(3):
                self.attn_qkvw[i * self.config.hidden_size:(i + 1) * self.config.hidden_size] = \
                    torch.empty_like(initial_weights[i]).copy_(initial_weights[i])
            self.attn_qkvb = nn.Parameter(torch.Tensor(self.config.hidden_size * 3))
            self.attn_qkvb.data.zero_()
            self.attn_ow = initial_weights[3]
            self.attn_ob = initial_biases[3]
            self.attn_nw = initial_weights[4]
            self.attn_nb = initial_biases[4]
            self.inter_w = initial_weights[5]
            self.inter_b = initial_biases[5]
            self.output_w = initial_weights[6]
            self.output_b = initial_biases[6]
            self.norm_w = initial_weights[7]
            self.norm_b = initial_biases[7]

        # create the layer in cuda kernels.
        cuda_module = ds_stochastic_transformer_cuda if self.config.stochastic_mode else ds_transformer_cuda
        create_layer_func = cuda_module.create_transformer_layer_fp16 if self.config.fp16 else cuda_module.create_transformer_layer_fp32

        create_layer_func(self.config.layer_id,
                          self.config.batch_size,
                          self.config.hidden_size,
                          self.config.heads,
                          4 * self.config.hidden_size,
                          self.config.max_seq_length,
                          self.config.attn_dropout_ratio,
                          self.config.hidden_dropout_ratio,
                          self.config.seed,
                          self.config.pre_layer_norm,
                          self.config.test_gemm,
                          self.config.attn_dropout_checkpoint,
                          self.config.normalize_invertible,
                          self.config.gelu_checkpoint,
                          self.config.stochastic_mode)

    def init_transformer_weights(self, adjust_init_range=False):
        num_layers = self.config.num_hidden_layers
        output_std = self.config.initializer_range
        if adjust_init_range and self.config.local_rank == 0:
            print("Accounting for accumulation on the residual path")
            output_std = self.config.initializer_range / math.sqrt(2.0 * num_layers)

        self.attn_qkvw.data.normal_(mean=0.0, std=self.config.initializer_range)
        self.attn_qkvb.data.zero_()
        self.attn_ow.data.normal_(mean=0.0, std=output_std)
        self.attn_ob.data.zero_()
        self.attn_nw.data.fill_(1.0)
        self.attn_nb.data.zero_()
        self.inter_w.data.normal_(mean=0.0, std=self.config.initializer_range)
        self.inter_b.data.zero_()
        self.output_w.data.normal_(mean=0.0, std=output_std)
        self.output_b.data.zero_()
        self.norm_w.data.fill_(1.0)
        self.norm_b.data.zero_()

    #def forward(self, input, input_mask, grads=None):
    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        output_attentions=False,
    ):
        self.config.training = self.training
        self.config.is_grad_enabled = torch.is_grad_enabled()
        # disable grad testing for now
        grads = None
        return DeepSpeedTransformerFunction.apply(hidden_states,
                                                  attention_mask,
                                                  self,
                                                  grads,
                                                  self.config.layer_id,
                                                  self.attn_qkvw,
                                                  self.attn_qkvb,
                                                  self.attn_ow,
                                                  self.attn_ob,
                                                  self.attn_nw,
                                                  self.attn_nb,
                                                  self.inter_w,
                                                  self.inter_b,
                                                  self.output_w,
                                                  self.output_b,
                                                  self.norm_w,
                                                  self.norm_b,
                                                  self.config)
