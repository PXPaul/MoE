import math
import torch
from torch import autograd
import torch.nn as nn
from torch.nn import init, CrossEntropyLoss, MSELoss
import torch.nn.functional as F
from transformers import AdamW, get_linear_schedule_with_warmup
from transformers.configuration_utils import PretrainedConfig
from transformers.modeling_utils import PreTrainedModel
from transformers.models.bert.modeling_bert import load_tf_weights_in_bert, ACT2FN

class MaskedDistilBertConfig(PretrainedConfig):
    model_type = "masked_distilbert"

    def __init__(
        self,
        vocab_size=30522,
        hidden_size=768,
        num_hidden_layers=6,  
        num_attention_heads=12,
        intermediate_size=3072,
        hidden_act="gelu",
        hidden_dropout_prob=0.1,
        attention_probs_dropout_prob=0.1,
        max_position_embeddings=512,
        initializer_range=0.02,
        layer_norm_eps=1e-12,
        pad_token_id=0,
        pruning_method="topK",
        mask_init="constant",
        mask_scale=0.0,
        **kwargs
    ):
        super().__init__(pad_token_id=pad_token_id, **kwargs)
        self.vocab_size = vocab_size
        self.hidden_size = hidden_size
        self.num_hidden_layers = num_hidden_layers
        self.num_attention_heads = num_attention_heads
        self.hidden_act = hidden_act
        self.intermediate_size = intermediate_size
        self.hidden_dropout_prob = hidden_dropout_prob
        self.attention_probs_dropout_prob = attention_probs_dropout_prob
        self.max_position_embeddings = max_position_embeddings
        self.initializer_range = initializer_range
        self.layer_norm_eps = layer_norm_eps
        self.pruning_method = pruning_method
        self.mask_init = mask_init
        self.mask_scale = mask_scale

class MaskedDistilBertPreTrainedModel(PreTrainedModel):
    config_class = MaskedDistilBertConfig
    load_tf_weights = load_tf_weights_in_bert
    base_model_prefix = "distilbert"

    def _init_weights(self, module):
        if isinstance(module, (nn.Linear, nn.Embedding)):
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
        elif isinstance(module, torch.nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)
        if isinstance(module, nn.Linear) and module.bias is not None:
            module.bias.data.zero_()

class MaskedDistilBertModel(MaskedDistilBertPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.config = config

        self.embeddings = DistilBertEmbeddings(config)
        self.embeddings.requires_grad_(requires_grad=False)
        self.encoder = DistilBertEncoder(config)

        self.init_weights()

    def get_input_embeddings(self):
        return self.embeddings.word_embeddings

    def set_input_embeddings(self, value):
        self.embeddings.word_embeddings = value

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        threshold=None,
    ):
        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            input_shape = input_ids.size()
        elif inputs_embeds is not None:
            input_shape = inputs_embeds.size()[:-1]
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        device = input_ids.device if input_ids is not None else inputs_embeds.device

        if attention_mask is None:
            attention_mask = torch.ones(input_shape, device=device)

        if attention_mask.dim() == 3:
            extended_attention_mask = attention_mask[:, None, :, :]
        elif attention_mask.dim() == 2:
            # Provided a padding mask of dimensions [batch_size, seq_length]
            # - if the model is a decoder, apply a causal mask in addition to the padding mask
            # - if the model is an encoder, make the mask broadcastable to [batch_size, num_heads, seq_length, seq_length]
            if self.config.is_decoder:
                batch_size, seq_length = input_shape
                seq_ids = torch.arange(seq_length, device=device)
                causal_mask = seq_ids[None, None, :].repeat(batch_size, seq_length, 1) <= seq_ids[None, :, None]
                causal_mask = causal_mask.to(
                    attention_mask.dtype
                )  # causal and attention masks must have same type with pytorch version < 1.3
                extended_attention_mask = causal_mask[:, None, :, :] * attention_mask[:, None, None, :]
            else:
                extended_attention_mask = attention_mask[:, None, None, :]
        else:
            raise ValueError(
                "Wrong shape for input_ids (shape {}) or attention_mask (shape {})".format(
                    input_shape, attention_mask.shape
                )
            )

        extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0
        
        if head_mask is not None:
            if head_mask.dim() == 1:
                head_mask = head_mask.unsqueeze(0).unsqueeze(0).unsqueeze(-1).unsqueeze(-1)
                head_mask = head_mask.expand(self.config.num_hidden_layers, -1, -1, -1, -1)
            elif head_mask.dim() == 2:
                head_mask = (
                    head_mask.unsqueeze(1).unsqueeze(-1).unsqueeze(-1)
                )  # We can specify head_mask for each layer
        else:
            head_mask = [None] * self.config.num_hidden_layers
            
        embedding_output = self.embeddings(
            input_ids=input_ids, position_ids=position_ids, inputs_embeds=inputs_embeds
        )
        encoder_outputs = self.encoder(
            embedding_output,
            attention_mask=extended_attention_mask,
            head_mask=head_mask,
            threshold=threshold,
        )
        sequence_output = encoder_outputs[0]

        outputs = (sequence_output,) + encoder_outputs[1:]
        return outputs  # sequence_output, (hidden_states), (attentions)

class DistilBertEmbeddings(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.word_embeddings = nn.Embedding(config.vocab_size, config.hidden_size, padding_idx=0)
        self.position_embeddings = nn.Embedding(config.max_position_embeddings, config.hidden_size)
        self.LayerNorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, input_ids=None, position_ids=None, inputs_embeds=None):
        if input_ids is not None:
            input_shape = input_ids.size()
        else:
            input_shape = inputs_embeds.size()[:-1]

        seq_length = input_shape[1]
        device = input_ids.device if input_ids is not None else inputs_embeds.device
        if position_ids is None:
            position_ids = torch.arange(seq_length, dtype=torch.long, device=device)
            position_ids = position_ids.unsqueeze(0).expand(input_shape)

        if inputs_embeds is None:
            inputs_embeds = self.word_embeddings(input_ids)
        position_embeddings = self.position_embeddings(position_ids)

        embeddings = inputs_embeds + position_embeddings
        embeddings = self.LayerNorm(embeddings)
        embeddings = self.dropout(embeddings)
        return embeddings

class DistilBertEncoder(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.output_attentions = config.output_attentions
        self.output_hidden_states = config.output_hidden_states
        self.layer = nn.ModuleList([DistilBertLayer(config) for _ in range(config.num_hidden_layers)])

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        threshold=None,
    ):
        all_hidden_states = ()
        all_attentions = ()
        for i, layer_module in enumerate(self.layer):
            if self.output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states,)

            layer_outputs = layer_module(
                hidden_states,
                attention_mask,
                head_mask[i],
                threshold=threshold,
            )
            hidden_states = layer_outputs[0]

            if self.output_attentions:
                all_attentions = all_attentions + (layer_outputs[1],)

        if self.output_hidden_states:
            all_hidden_states = all_hidden_states + (hidden_states,)

        outputs = (hidden_states,)
        if self.output_hidden_states:
            outputs = outputs + (all_hidden_states,)
        if self.output_attentions:
            outputs = outputs + (all_attentions,)
        return outputs

class DistilBertLayer(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.attention = DistilBertAttention(config)
        self.intermediate = DistilBertIntermediate(config)
        self.output = DistilBertOutput(config)

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        threshold=None,
    ):
        self_attention_outputs = self.attention(hidden_states, attention_mask, head_mask, threshold=threshold)
        attention_output = self_attention_outputs[0]
        outputs = self_attention_outputs[1:]

        intermediate_output = self.intermediate(attention_output, threshold=threshold)
        layer_output = self.output(intermediate_output, attention_output, threshold=threshold)
        outputs = (layer_output,) + outputs
        return outputs

class DistilBertAttention(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.self = DistilBertSelfAttention(config)
        self.output = DistilBertSelfOutput(config)

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        threshold=None,
    ):
        self_outputs = self.self(
            hidden_states,
            attention_mask,
            head_mask,
            threshold=threshold,
        )
        attention_output = self.output(self_outputs[0], hidden_states, threshold=threshold)
        outputs = (attention_output,) + self_outputs[1:]
        return outputs

class DistilBertSelfAttention(nn.Module):
    def __init__(self, config):
        super().__init__()
        if config.hidden_size % config.num_attention_heads != 0 and not hasattr(config, "embedding_size"):
            raise ValueError(
                "The hidden size (%d) is not a multiple of the number of attention "
                "heads (%d)" % (config.hidden_size, config.num_attention_heads)
            )
        self.output_attentions = config.output_attentions

        self.num_attention_heads = config.num_attention_heads
        self.attention_head_size = int(config.hidden_size / config.num_attention_heads)
        self.all_head_size = self.num_attention_heads * self.attention_head_size

        self.query = MaskedLinear(
            config.hidden_size,
            self.all_head_size,
            pruning_method=config.pruning_method,
            mask_init=config.mask_init,
            mask_scale=config.mask_scale,
        )
        self.key = MaskedLinear(
            config.hidden_size,
            self.all_head_size,
            pruning_method=config.pruning_method,
            mask_init=config.mask_init,
            mask_scale=config.mask_scale,
        )
        self.value = MaskedLinear(
            config.hidden_size,
            self.all_head_size,
            pruning_method=config.pruning_method,
            mask_init=config.mask_init,
            mask_scale=config.mask_scale,
        )

        self.dropout = nn.Dropout(config.attention_probs_dropout_prob)

    def transpose_for_scores(self, x):
        new_x_shape = x.size()[:-1] + (self.num_attention_heads, self.attention_head_size)
        x = x.view(*new_x_shape)
        return x.permute(0, 2, 1, 3)

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        threshold=None,
    ):
        mixed_query_layer = self.query(hidden_states, threshold=threshold)

        # If this is instantiated as a cross-attention module, the keys
        # and values come from an encoder; the attention mask needs to be
        # such that the encoder's padding tokens are not attended to.
        if encoder_hidden_states is not None:
            mixed_key_layer = self.key(encoder_hidden_states, threshold=threshold)
            mixed_value_layer = self.value(encoder_hidden_states, threshold=threshold)
            attention_mask = encoder_attention_mask
        else:
            mixed_key_layer = self.key(hidden_states, threshold=threshold)
            mixed_value_layer = self.value(hidden_states, threshold=threshold)

        query_layer = self.transpose_for_scores(mixed_query_layer)
        key_layer = self.transpose_for_scores(mixed_key_layer)
        value_layer = self.transpose_for_scores(mixed_value_layer)

        # Take the dot product between "query" and "key" to get the raw attention scores.
        attention_scores = torch.matmul(query_layer, key_layer.transpose(-1, -2))
        attention_scores = attention_scores / math.sqrt(self.attention_head_size)
        if attention_mask is not None:
            # Apply the attention mask is (precomputed for all layers in BertModel forward() function)
            attention_scores = attention_scores + attention_mask

        # Normalize the attention scores to probabilities.
        attention_probs = nn.Softmax(dim=-1)(attention_scores)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        attention_probs = self.dropout(attention_probs)

        # Mask heads if we want to
        if head_mask is not None:
            attention_probs = attention_probs * head_mask

        context_layer = torch.matmul(attention_probs, value_layer)

        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()
        new_context_layer_shape = context_layer.size()[:-2] + (self.all_head_size,)
        context_layer = context_layer.view(*new_context_layer_shape)

        outputs = (context_layer, attention_probs) if self.output_attentions else (context_layer,)
        return outputs



class MaskedLinear(nn.Linear):
    def __init__(
        self,
        in_features: int,
        out_features: int,
        bias: bool = True,
        mask_init: str = 'constant',
        mask_scale: float = 0.0,
        pruning_method: str = 'topK',
    ):
      
        super(MaskedLinear, self).__init__(in_features=in_features, out_features=out_features, bias=bias)
        self.pruning_method = pruning_method
        self.mask_scale = mask_scale
        self.mask_init = mask_init

        ### randn
        self.mask_scores_1 = nn.Parameter(torch.randn(self.weight.size()))
        self.mask_scores_2 = nn.Parameter(torch.randn(self.weight.size()))
        self.mask_scores_3 = nn.Parameter(torch.randn(self.weight.size()))
        
        self.num_masks = 3

        self.gate_lin = nn.Linear(in_features, self.num_masks)

    def forward(self, input: torch.tensor, threshold: float):
        mask1 = TopKBinarizer.apply(self.mask_scores_1, threshold)
        mask2 = TopKBinarizer.apply(self.mask_scores_2, threshold)
        mask3 = TopKBinarizer.apply(self.mask_scores_3, threshold)
        

        gate_scores = self.gate_lin(input)
        selected_mask_index = torch.argmax(gate_scores, dim=-1, keepdim=True).expand(-1, -1, input.size(-1))

        output = torch.zeros((*input.shape[:-1], self.weight.shape[0]), device=input.device)
        
        for mask_index, mask in enumerate([mask1, mask2, mask3]):
           # This mask selects the inputs that are relevant to this mask
           element_mask = selected_mask_index == mask_index 
           # Select which inputs are relevant to the mask we are using this iteration
           relevant_inputs = torch.where(element_mask, input, 0) 
           # Masked weight for iteration
           masked_weight = mask * self.weight 
           # We only write the relevant rows at each iteration so ok to just add
           output += relevant_inputs @ masked_weight.T 

        output += self.bias
    
        return output
    

class DistilBertSelfOutput(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        self.LayerNorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
#         self.LayerNorm = BertLayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, hidden_states, input_tensor, threshold):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        return hidden_states
    
class DistilBertIntermediate(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.dense = MaskedLinear(
            config.hidden_size,
            config.intermediate_size,
            pruning_method=config.pruning_method,
            mask_init=config.mask_init,
            mask_scale=config.mask_scale,
        )
        if isinstance(config.hidden_act, str):
            self.intermediate_act_fn = ACT2FN[config.hidden_act]
        else:
            self.intermediate_act_fn = config.hidden_act

    def forward(self, hidden_states, threshold):
        hidden_states = self.dense(hidden_states, threshold=threshold)
        hidden_states = self.intermediate_act_fn(hidden_states)
        return hidden_states

# Cell
class DistilBertOutput(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Linear(config.intermediate_size, config.hidden_size)
        self.LayerNorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
#         self.LayerNorm = BertLayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, hidden_states, input_tensor, threshold):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        return hidden_states


class TopKBinarizer(autograd.Function):
    """
    Top-k Binarizer.
    Computes a binary mask M from a real value matrix S such that `M_{i,j} = 1` if and only if `S_{i,j}`
    is among the k% highest values of S.
    """
    @staticmethod
    def forward(ctx, inputs: torch.tensor, threshold: float):
        # Get the subnetwork by sorting the inputs and using the top threshold %
        if not isinstance(threshold, float):
            threshold = threshold[0]
        mask = inputs.clone() ###
        _, idx = inputs.flatten().sort(descending=True)
        j = int(threshold * inputs.numel())

        # flat_out and mask access the same memory.
        flat_out = mask.flatten()
        flat_out[idx[j:]] = 0
        flat_out[idx[:j]] = 1
        return mask

    @staticmethod
    def backward(ctx, gradOutput):
        return gradOutput, None
    

class MaskedDistilBertForSequenceClassification(MaskedDistilBertPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.num_labels = config.num_labels

        self.distilbert = MaskedDistilBertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, self.config.num_labels)

        self.init_weights()

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        labels=None,
        threshold=None,
    ):

        outputs = self.distilbert(
            input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            threshold=threshold,
        )

        sequence_output = outputs[0]
        pooled_output = sequence_output[:, 0]  # Use the first token (CLS token)

        pooled_output = self.dropout(pooled_output)
        logits = self.classifier(pooled_output)

        outputs = (logits,) + outputs[2:]

        if labels is not None:
            if self.num_labels == 1:
                loss_fct = MSELoss()
                loss = loss_fct(logits.view(-1), labels.view(-1))
            else:
                loss_fct = CrossEntropyLoss()
                loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))
            outputs = (loss,) + outputs

        return outputs

class MaskedDistilBertForQuestionAnswering(MaskedDistilBertPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.num_labels = config.num_labels
        self.distilbert = MaskedDistilBertModel(config)
        self.qa_outputs = nn.Linear(config.hidden_size, config.num_labels)
        self.init_weights()

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        head_mask=None,
        inputs_embeds=None,
        start_positions=None,
        end_positions=None,
        threshold=None,
    ):
        # Forward pass through the DistilBert model
        outputs = self.distilbert(
            input_ids=input_ids,
            attention_mask=attention_mask,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
        )

        sequence_output = outputs[0]

        # Compute logits for start and end positions
        logits = self.qa_outputs(sequence_output)
        start_logits, end_logits = logits.split(1, dim=-1)
        start_logits = start_logits.squeeze(-1)
        end_logits = end_logits.squeeze(-1)

        outputs = (
            start_logits,
            end_logits,
        ) + outputs[1:]  # DistilBert outputs do not include pooled_output

        if start_positions is not None and end_positions is not None:
            if len(start_positions.size()) > 1:
                start_positions = start_positions.squeeze(-1)
            if len(end_positions.size()) > 1:
                end_positions = end_positions.squeeze(-1)
            ignored_index = start_logits.size(1)
            start_positions.clamp_(0, ignored_index)
            end_positions.clamp_(0, ignored_index)

            loss_fct = CrossEntropyLoss(ignore_index=ignored_index)
            start_loss = loss_fct(start_logits, start_positions)
            end_loss = loss_fct(end_logits, end_positions)
            total_loss = (start_loss + end_loss) / 2
            outputs = (total_loss,) + outputs

        return outputs  # (loss), start_logits, end_logits, (hidden_states), (attentions)