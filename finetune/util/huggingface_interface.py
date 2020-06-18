import h5py
import numpy as np
import joblib as jl
import os
import tqdl
import tensorflow as tf
from tensorflow.python.keras.saving.hdf5_format import load_attributes_from_hdf5_group
from finetune.util.download import FINETUNE_BASE_FOLDER
from finetune.util.shapes import lengths_from_eos_idx
from finetune.encoding.input_encoder import BaseEncoder
from finetune.encoding.input_encoder import EncodedOutput
from finetune.base_models import SourceModel
from finetune.optimizers.recompute_grads import recompute_grad

from tensorflow.python.util import tf_inspect


def load_weights_from_hdf5_group_by_name(filepath, weights_replacement):
    with h5py.File(filepath, "r") as f:
        if "layer_names" not in f.attrs and "model_weights" in f:
            f = f["model_weights"]

        # New file format.
        layer_names = load_attributes_from_hdf5_group(f, "layer_names")

        weight_lookup = {}
        for name in layer_names:
            g = f[name]
            for name in load_attributes_from_hdf5_group(g, "weight_names"):
                output_name = name
                for fro, to in weights_replacement:
                    output_name = output_name.replace(fro, to)
                weight_lookup[output_name] = np.asarray(g[name])
    return weight_lookup


def finetune_model_from_huggingface(
    pretrained_weights,
    archive_map,
    hf_featurizer,
    hf_tokenizer,
    hf_config,
    weights_replacement,
):
    weights_url = archive_map[pretrained_weights]
    hf_tokenizer_instance = hf_tokenizer.from_pretrained(pretrained_weights)
    hf_config_instance = hf_config.from_pretrained(pretrained_weights)

    def finetune_featurizer(X, encoder, config, train=False, reuse=None, **kwargs):
        initial_shape = tf.shape(input=X)
        X = tf.reshape(X, shape=tf.concat(([-1], initial_shape[-1:]), 0))
        X.set_shape([None, None])
        delimiters = tf.cast(tf.equal(X, encoder.delimiter_token), tf.int32)

        token_type_ids = tf.cumsum(delimiters, exclusive=True, axis=1)

        seq_length = tf.shape(input=delimiters)[1]

        eos_idx = tf.argmax(
            input=tf.cast(delimiters, tf.float32)
            * tf.expand_dims(
                tf.range(tf.cast(seq_length, tf.float32), dtype=tf.float32), 0
            ),
            axis=1,
        )

        lengths = lengths_from_eos_idx(eos_idx=eos_idx, max_length=seq_length)
        mask = tf.sequence_mask(lengths, maxlen=seq_length, dtype=tf.float32)

        with tf.compat.v1.variable_scope("model/featurizer", reuse=reuse):
            hf_model = hf_featurizer(hf_config_instance)

            if config.low_memory_mode and train:
                for layer in hf_model.encoder.layer:
                    layer.__call__ == recompute_grad(
                        layer.__call__, train_vars=layer.trainable_weights
                    )

            kwargs = {
                "attention_mask": mask,
                "token_type_ids": token_type_ids,
                "inputs_embeds": None,
                "training": train,
            }
            call_args = tf_inspect.getargspec(hf_model).args
            kwargs = {k: v for k, v in kwargs.items() if k in call_args}

            model_out = hf_model(X, **kwargs)
            if isinstance(model_out, tuple) and len(model_out) > 1:
                sequence_out, pooled_out, *_ = model_out
            else:
                sequence_out = model_out[0]
                pooled_out = sequence_out[:, 0, :]

            n_embed = pooled_out.shape[-1]

            embed_weights = hf_model.embeddings.word_embeddings
            features = tf.reshape(
                pooled_out, shape=tf.concat((initial_shape[:-1], [n_embed]), 0),
            )
            sequence_features = tf.reshape(
                sequence_out, shape=tf.concat((initial_shape, [n_embed]), 0),
            )

            output_state = {
                "embed_weights": embed_weights,
                "features": features,
                "sequence_features": sequence_features,
                "lengths": lengths,
                "eos_idx": eos_idx,
            }

            return output_state

    class HuggingFaceEncoder(BaseEncoder):
        def __init__(self):
            self.tokenizer = hf_tokenizer_instance

            self.start_token = self.tokenizer.cls_token_id
            self.delimiter_token = (
                self.tokenizer.sep_token_id or self.tokenizer.eos_token_id
            )
            self.mask_token = self.tokenizer.mask_token_id
            self.end_token = self.tokenizer.eos_token_id or self.delimiter_token
            self.UNK_IDX = self.tokenizer.unk_token_id
            self.initialized = True

        @property
        def vocab_size(self):
            self.tokenizer.vocab_size

        def _encode(self, texts):
            batch_tokens = []
            batch_token_idxs = []
            batch_char_ends = []
            batch_char_starts = []
            for i, text in enumerate(texts):
                encoded = self.tokenizer._tokenizer.encode(
                    text, add_special_tokens=False
                )
                batch_tokens.append(encoded.tokens)
                batch_token_idxs.append(encoded.ids)
                token_ends = []
                token_starts = []
                for start, end in encoded.offsets:
                    token_starts.append(start)
                    token_ends.append(end)

                batch_char_ends.append(token_ends)
                batch_char_starts.append(token_starts)

            return EncodedOutput(
                token_ids=batch_token_idxs,
                tokens=batch_tokens,
                token_ends=batch_char_ends,
                token_starts=batch_char_starts,
            )

        def decode(self, ids):
            return NotImplemented

    weights_file = "{}_{}.jl".format(
        pretrained_weights.replace("/", "_"), hf_featurizer.__name__
    )
    raw_weights_path = os.path.join(
        FINETUNE_BASE_FOLDER, "model", "huggingface", "raw_" + weights_file
    )
    weights_path = os.path.join(
        FINETUNE_BASE_FOLDER, "model", "huggingface", weights_file
    )

    class HuggingFaceModel(SourceModel):
        encoder = HuggingFaceEncoder
        featurizer = finetune_featurizer
        settings = {
            "base_model_path": os.path.join("huggingface", weights_file),
            "n_layer": 12,
            "n_embed": hf_config_instance.hidden_size,
            "max_length": hf_config_instance.max_position_embeddings,
        }
        required_files = [{"url": weights_url, "file": raw_weights_path}]

        @classmethod
        def translate_base_model_format(cls):
            jl.dump(
                load_weights_from_hdf5_group_by_name(
                    raw_weights_path, weights_replacement
                ),
                weights_path,
            )

        def __reduce__(self):
            return (
                finetune_model_from_huggingface,
                (
                    pretrained_weights,
                    archive_map,
                    hf_featurizer,
                    hf_tokenizer,
                    hf_config,
                    weights_replacement,
                ),
            )

    # Note: we don't usually handle an instance of this, but just the class it's self.
    #  But we need to here else we can't pickle it....
    return HuggingFaceModel()