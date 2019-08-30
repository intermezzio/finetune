import unittest
import os

import pandas as pd
from sklearn.model_selection import train_test_split

from finetune.base_models import OSCAR
from finetune import LMPred, Classifier
from finetune.util.metrics import read_eval_metrics


class TestOscarFeatures(unittest.TestCase):

    def test_lm_predict(self):
        """
        Ensure LM only training does not error out
        """
        model = LMPred(base_model=OSCAR, max_length=32, batch_size=1, beam_size=1, decoder_sample_from=0, sample_temp=0.)
        model.fit(["This is the beginning of this test sentence."] * 100)
        predictions = model.predict(["This is the beginning of "] * 5)
        for pred in predictions:
            self.assertIsInstance(pred, str)
            self.assertIn("This is the beginning of", pred)

    def test_in_memory_finetune(self):
        SST_FILENAME = "SST-binary.csv"
        DATA_PATH = os.path.join('Data', 'Classify', SST_FILENAME)
        dataframe = pd.read_csv(DATA_PATH, nrows=100).dropna()
        trainX, testX, trainY, testY = train_test_split(list(dataframe.Text.values), list(dataframe.Target.values),
                                                        test_size=0.3, random_state=42)
        in_memory_finetune = [
            {
                "config": {"n_epochs": 1, "max_length": 64},
                "X": trainX,
                "Y": trainY,
                "X_test": testX,
                "Y_test": testY,
                "name": "sst-b",
                "every_n_iter": 30
            }
        ]
        model = Classifier(in_memory_finetune=in_memory_finetune, max_length=64)
        model.fit(trainX, trainY)
        metrics = read_eval_metrics(os.path.join(model.estimator_dir, "finetuning"))
        for step, metric in metrics.items():
            self.assertEqual(len(metric), 2) # train and test
            for key, value in metric:
                self.assertGreaterEqual(value, 0)
                self.assertLessEqual(value, 1)
                self.assertIn("finetuning", key)
