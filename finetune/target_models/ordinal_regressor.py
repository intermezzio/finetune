import tensorflow as tf
import numpy as np
from scipy.stats import mode

from finetune.base import BaseModel
from finetune.encoding.target_encoders import OrdinalRegressionEncoder
from finetune.nn.target_blocks import ordinal_regressor
from finetune.input_pipeline import BasePipeline
from finetune.target_models.comparison_regressor import ComparisonRegressionPipeline
from finetune.target_models.comparison import Comparison


class OrdinalRegressionPipeline(BasePipeline):
    def _target_encoder(self):
        return OrdinalRegressionEncoder()


class ComparisonOrdinalRegressionPipeline(ComparisonRegressionPipeline):
    def _target_encoder(self):
        return OrdinalRegressionEncoder()


class OrdinalRegressor(BaseModel):
    """
    Classifies a document into two or more ordered categories.

    For a full list of configuration options, see `finetune.config`.

    :param config: A config object generated by `finetune.config.get_config` or None (for default config).
    :param \**kwargs: key-value pairs of config items to override.
    """

    def __init__(self, shared_threshold_weights=True, **kwargs):
        super().__init__(**kwargs)
        self.config.shared_threshold_weights = shared_threshold_weights

    def _get_input_pipeline(self):
        return OrdinalRegressionPipeline(self.config)

    def featurize(self, X):
        """
        Embeds inputs in learned feature space. Can be called before or after calling :meth:`finetune`.

        :param X: list or array of text to embed.
        :returns: np.array of features of shape (n_examples, embedding_size).
        """
        return self._featurize(X)

    def predict(self, X, context=None):
        """
        Produces a list of most likely class labels as determined by the fine-tuned model.

        :param X: list or array of text to embed.
        :returns: list of class labels.
        """
        all_labels = []
        for _, start_of_doc, end_of_doc, label, _ in self.process_long_sequence(X, context=context):
            if start_of_doc:
                # if this is the first chunk in a document, start accumulating from scratch
                doc_labels = []

            doc_labels.append(label)

            if end_of_doc:
                # last chunk in a document
                # since we are working with discrete boundaries, and have no probabilities to average across, we take the mode                
                doc_labels = np.asarray(doc_labels)
                doc_mode = mode(doc_labels).mode
                label = self.input_pipeline.label_encoder.inverse_transform(np.asarray([doc_mode]))
                all_labels.append(label.tolist())
        return all_labels

    def predict_proba(self, X, context=None):
        """
        Produces a probability distribution over classes for each example in X.

        :param X: list or array of text to embed.
        :returns: list of dictionaries.  Each dictionary maps from a class label to its assigned class probability.
        """
        raise AttributeError("`Regressor` model does not support `predict_proba`.")

    def finetune(self, X, Y=None, batch_size=None, context=None):
        """
        :param X: list or array of text.
        :param Y: floating point targets
        :param batch_size: integer number of examples per batch. When N_GPUS > 1, this number
                           corresponds to the number of training examples provided to each GPU.
        """
        return super().finetune(X, Y=Y, batch_size=batch_size, context=context)

    def _target_model(
        self, *, config, featurizer_state, targets, n_outputs, train=False, reuse=None, **kwargs
    ):
        self._add_context_embed(featurizer_state)
        return ordinal_regressor(
            hidden=featurizer_state["features"],
            targets=targets,
            n_targets=n_outputs,
            config=config,
            train=train,
            reuse=reuse,
            shared_threshold_weights=config.shared_threshold_weights,
            **kwargs
        )

    def _predict_op(self, logits, **kwargs):
        return logits

    def _predict_proba_op(self, logits, **kwargs):
        return logits


class ComparisonOrdinalRegressor(OrdinalRegressor):
    """
    Compares two documents and classifies into two or more ordered categories.

    For a full list of configuration options, see `finetune.config`.

    :param config: A config object generated by `finetune.config.get_config` or None (for default config).
    :param \**kwargs: key-value pairs of config items to override.
    """
    defaults = {"chunk_long_sequences": False}

    def predict(self, pairs):
        """
        Produces a floating point prediction determined by the fine-tuned model.


        :param pairs: Array of text, shape [batch, 2]
        :returns: list of floats, shape [batch]
        """
        return list(Comparison.predict(self, pairs))


    def _get_input_pipeline(self):
        return ComparisonOrdinalRegressionPipeline(self.config)

    def _target_model(
        self, *, config, featurizer_state, targets, n_outputs, train=False, reuse=None, **kwargs
    ):
        featurizer_state["sequence_features"] = tf.abs(
            tf.reduce_sum(featurizer_state["sequence_features"], 1)
        )
        featurizer_state["features"] = tf.abs(
            tf.reduce_sum(featurizer_state["features"], 1)
        )
        if 'context' in featurizer_state:
            featurizer_state["context"] = tf.abs(
                tf.reduce_sum(featurizer_state["context"], 1)
            )
        return super(ComparisonOrdinalRegressor, self)._target_model(
            config=config,
            featurizer_state=featurizer_state,
            targets=targets,
            n_outputs=n_outputs,
            train=train,
            reuse=reuse,
            **kwargs
        )

