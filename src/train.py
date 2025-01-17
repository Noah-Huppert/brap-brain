import argparse
import os
import sys
import json
import threading
from typing import Tuple

from aitextgen import aitextgen
from aitextgen.TokenDataset import TokenDataset
from aitextgen.utils import build_gpt2_config
from transformers import GPT2Config 

import lib_logging
from lib_path import LocalPath
from build_tokenizer import TokenizerConfig

logger = lib_logging.make_logger('train')

class TrainingMetadata:
    """ Holds metadata about the training progress of a model.
    Fields:
    - training_iterations: Number of training steps taken by the model.
    - tokenizer_index: Path to tokenizer index file which contains information about the tokenizer used to train the model.
    """
    training_iterations: int
    tokenizer_index: LocalPath

    def __init__(
        self,
        training_iterations: int,
        tokenizer_index: LocalPath,
    ):
        """ Initialize the TrainingMetadata.
        Arguments:
        - training_iterations: See TrainingMetadata.training_iterations
        - tokenizer_index: See TrainingMetadata.tokenizer_index
        """
        self.training_iterations = training_iterations
        self.tokenizer_index = tokenizer_index

    def save(self, out_file: LocalPath):
        """ Save the training metadata to a JSON file.
        Arguments:
        - out_file: Path to which JSON file will be saved
        """
        with open(out_file.get_absolute_path(), 'w') as out_f:
            json.dump(
                {
                    'training_iterations': self.training_iterations,
                    'tokenizer_index': self.tokenizer_index.get_project_relative_path(),
                },
                out_f,
                indent=4,
            )

    @staticmethod
    def load(in_file: LocalPath) -> 'TrainingMetadata':
        """ Load training metadata from a JSON file.
        Arguments:
        - in_file: Path to JSON file which will be loaded

        Returns: TrainingMetadata loaded from file

        Raises:
        - KeyError: If JSON file doesn't contain value required
        """
        with open(in_file.get_absolute_path(), 'r') as in_f:
            data = json.load(in_f)

            return TrainingMetadata(
                training_iterations=data['training_iterations'],
                tokenizer_index=LocalPath(data['tokenizer_index']),
            )


def parse_args():
    parser = argparse.ArgumentParser(description="Train the model")
    parser.add_argument(
        '--gpu',
        help="Train the model on a GPU",
        action='store_true',
        default=False,
    )

    parser.add_argument(
        '--tokenizer-index',
        help="Path to tokenizer input file which specifies which tokenizer to use",
        type=LocalPath,
        default=LocalPath("training-data/tokenizer-index.json"),
    )
   
    parser.add_argument(
        '--dataset',
        help="Path to training dataset file",
        type=LocalPath,
        default=LocalPath("training-data/discord-messages.txt"),
    )

    parser.add_argument(
        '--models-dir',
        help="Parent directory where models will be saved",
        type=LocalPath,
        default=LocalPath("models"),
    )
    parser.add_argument(
        '--model-name',
        help="Name of model, used to determine where it will be saved",
        type=str,
        default="model",
    )

    parser.add_argument(
        '--sample-every',
        help="The number of training steps which should occur between model output samples",
        type=int,
        default=100,
    )

    parser.add_argument(
        '--target-epochs',
        help="Number of training epochs to run, -1 to continuously train with no limit",
        type=int,
        default=-1
    )

    parser.add_argument(
        '--train-epoch-steps',
        help="Number of steps of training to perform for each overall cycle of training",
        type=int,
        default=100,
    )

    parser.add_argument(
        '--checkpoint-every-epochs',
        help="Save a checkpoint of the trained result every number of epochs",
        type=int,
        default=5,
    )

    return parser.parse_args()

def main():
    # Parse arguments
    args = parse_args()
    
    tokenizer_config = TokenizerConfig.load(args.tokenizer_index)

    # Build dataset
    (data, config) = load_dataset(
        tokenizer_config=tokenizer_config,
        dataset=args.dataset,
    )

    # Train
    train(
        data=data,
        output_parent_dir=args.models_dir,
        model_name=args.model_name,
        config=config,
        tokenizer_index=args.tokenizer_index,
        tokenizer_config=tokenizer_config,
        gpu=args.gpu,
        sample_every=args.sample_every,
        target_epochs=args.target_epochs,
        train_epoch_steps=args.train_epoch_steps,
        checkpoint_every_epochs=args.checkpoint_every_epochs,
    )

def load_dataset(
    tokenizer_config: TokenizerConfig,
    dataset: LocalPath,
) -> Tuple[TokenDataset, GPT2Config]:
    """ Encode training dataset and create a GPT-2 configuration which targets the dataset.
    Arguments:
    - tokenizer_config: Parameters of the Tokenizer which should be used to encode dataset
    - dataset: Path to dataset file

    Returns: (Tokenizer dataset, Configuration for a GPT-2 model preset to use the tokenized dataset)
    """
    # Check if encoded cache exists
    encoded_dataset_path = os.path.splitext(dataset.get_absolute_path())[0] + '.tar.gz'
    dataset_kwargs = {}

    if os.path.exists(encoded_dataset_path):
        dataset_kwargs['from_cache'] = encoded_dataset_path
    
    # Load dataset
    data = TokenDataset(
        file_path=dataset.get_absolute_path(),
        vocab_file=tokenizer_config.vocab_file.get_absolute_path(),
        merges_file=tokenizer_config.merges_file.get_absolute_path(),
        #**dataset_kwargs,
    )
    config = build_gpt2_config(vocab_size=tokenizer_config.vocab_size)

    return (data, config)

def train(
    data: TokenDataset,
    output_parent_dir: LocalPath,
    model_name: str,
    config: GPT2Config,
    tokenizer_index: LocalPath,
    tokenizer_config: TokenizerConfig,
    gpu: bool,
    sample_every: int,
    target_epochs: int,
    train_epoch_steps: int,
    checkpoint_every_epochs: int,
):
    """ Train the model.
    Arguments:
    - data: Training dataset to feed to the model
    - output_parent_dir: Directory in which model sub-directories can be found
    - model_name: Name of sub-directory inside output_parent_dir in which model training data will be stored
    - config: AITextGen GPT-2 model configuration to use for training
    - tokenizer_index: Path to file which contains tokenizer parameters
    - tokenizer_config: Parameters of tokenizer loaded from tokenizer_index
    - gpu: If the model should be trained on a GPU
    - sample_every: How many training iterations should pass between samples being taken from the model being trained
    - target_epochs: The number of epochs of training which should be completed, set to a negative number to train infinitely
    - train_epoch_steps: The number of training iterations which should be completed in each epoch, this is also the number of steps which will be completed before the logic checks if training should gracefully exit
    - checkpoint_every_epochs: The number of epochs which should pass between backups of the model being made in the model directory
    """
    # Create model
    model = aitextgen(
        tokenizer_file=tokenizer_config.tokenizer_model_overview_file.get_absolute_path(),
        config=config,
        to_gpu=gpu,
    )

    def do_training(should_graceful_exit: threading.Event):
        """ Logic which runs training.
        Arguments:
        - should_graceful_exit: If set the model should stop training
        """

        # Model storage
        output_dir = output_parent_dir.join([model_name])
        logger.info(f"Saving model into '{output_dir.get_project_relative_path()}' directory")

        # Training metadata
        training_meta_path = output_dir.join(["training-metadata.json"])
        training_meta = TrainingMetadata(
            training_iterations=0,
            tokenizer_index=tokenizer_index,
        )

        if os.path.exists(training_meta_path.get_absolute_path()):
            training_meta = TrainingMetadata.load(training_meta_path)

            if training_meta.tokenizer_index != tokenizer_index:
                logger.warn(f"Training metadata specified a different tokenizer index file than invocation, stored value='{training_meta.tokenizer_index}', current value='{tokenizer_index}'")
                training_meta.tokenizer_index = tokenizer_index
    

        # Do training
        num_epochs = 0
        
        try:
            num_epochs_since_checkpoint = 0

            # Loop until the graceful exit flag is set or the number of target epochs is met (If there even is a number of target epochs)
            while not should_graceful_exit.is_set() and (num_epochs < target_epochs or target_epochs < 0):
                # Run one epoch of training
                model.train(
                    output_dir=output_dir.get_absolute_path(),
                    train_data=data,
                    num_workers=1, # Required or else training on GPUs won't work
                    generate_every=sample_every,
                    num_steps=train_epoch_steps,
                    progress_bar_refresh_rate=1
                )

                # Increment counters
                num_epochs += 1
                num_epochs_since_checkpoint += 1
                training_meta.training_iterations += train_epoch_steps

                logger.info(f"Completed training epoch {num_epochs} resulting in {train_epoch_steps} steps of training, total steps {training_meta.training_iterations}")
                
                training_meta.save(training_meta_path)

                # If we should make a checkpoint
                if num_epochs_since_checkpoint >= checkpoint_every_epochs:
                    checkpoint_dir = output_dir.join([f"checkpoint-{training_meta.training_iterations}"])
                    model.save(checkpoint_dir)
                    
                    training_meta.save(checkpoint_dir.join(["training-metadata.json"]))
                    logger.info(f"Model checkpoint saved into '{checkpoint_dir.get_project_relative_path()}' directory")

                    num_epochs_since_checkpoint = 0
        except Exception as e:
            logger.error("Failed to train model", e)
        finally:
            if should_graceful_exit.is_set():
                logger.info("Training gracefully shut down")
            
            training_meta.save(training_meta_path)
                    
            logger.info(f"Completed {num_epochs} epochs of training resulting in {train_epoch_steps * num_epochs} steps of training, total steps {training_meta.training_iterations}")
            logger.info(f"Model saved into '{output_dir.get_project_relative_path()}' directory")

    # Run training with graceful shutdown
    should_graceful_exit = threading.Event()
    training_thread = threading.Thread(
        target=do_training,
        kwargs={
            'should_graceful_exit': should_graceful_exit,
        },
    )

    def do_management(should_graceful_exit: threading.Event):
        """ Allow user to gracefully exit the training process.
        Arguments:
        - should_graceful_exit: Used to signal the training thread that it should stop
        """

        logger.info("Type 'quit' to stop training")
        training_thread.start()

        while True:
            user_input = input().strip()

            if user_input == 'quit':
                logger.info("Attempting a graceful shutdown")
                should_graceful_exit.set()
                return
            elif user_input == 'help':
                logger.info("While the model is training the following management commands are available")
                logger.info("- quit: At the end of the next training epoch gracefully end training")
            else:
                logger.info(f"Unrecognized user input command '{user_input}', type 'help' to see valid commands")

        logger.info("Waiting for training to gracefully shut down")
        training_thread.join()

    management_thread = threading.Thread(
        target=do_management,
        kwargs={
            'should_graceful_exit': should_graceful_exit,
        },
    )
    management_thread.start()
    
    training_thread.join()
    management_thread.join()

    logger.info("Done")

if __name__ == '__main__':
    main()
