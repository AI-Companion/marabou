import os
import matplotlib.pyplot as plt
import numpy as np
import tensorflow as tf
from marabou.training.datasets import KaggleDataset
from marabou.commons import NER_CONFIG_FILE, MODELS_DIR, NERConfigReader


def save_perf(model_name, history):
    """
    plots learning curve and saved under model directory with the given model name
    Args:
        model_name: model configuration name as indicated in the json config file
        history: dictionary containing model performance for each iteration
    Return:
        None

    """
    fig, axs = plt.subplots(1, 2)
    fig.tight_layout()
    acc = history['accuracy']
    val_acc = history['val_accuracy']
    loss = history['loss']
    val_loss = history['val_loss']
    epochs = range(1, len(acc) + 1)

    axs[0].plot(epochs, loss, 'bo', label='Training loss')
    axs[0].plot(epochs, val_loss, 'b', label='Validation loss')
    axs[0].set_title('Training and validation loss')
    axs[0].set_xlabel('Epochs')
    axs[0].set_ylabel('Loss')
    axs[0].legend()

    axs[1].plot(epochs, acc, 'bo', label='Training accuracy')
    axs[1].plot(epochs, val_acc, 'b', label='Validation accuracy')
    axs[1].set_title('Training and validation accuracy')
    axs[1].set_xlabel('Epochs')
    axs[1].set_ylabel('Accuracy')
    axs[1].legend(loc='lower right')

    output_file_name = os.path.join(MODELS_DIR, model_name)
    plt.savefig(output_file_name)


class PostProcessor(tf.keras.layers.Layer):
    """
    The postprocessing layer is used after model training finish
    Instead of returning class probabilities for each text token in each
    input text, the class will return asssigned class label for each token
    in each text input.
    The layer combines input from the input layer and the probilities from the last layer
    """
    def __init__(self, labels, **kwargs):
        self.labels = labels
        super().__init__(**kwargs)

    @tf.function
    def call(self, input_list, **kwargs):
        """
        implements the mapping from probabilities to class labels
        Args:
            input_list: a list of the input layer and output probabilities
        Return:
            mapped class labels for each input
        """
        preds = input_list[0]
        inputs = input_list[1]
        classes = tf.math.argmax(preds, axis=2)
        classes = tf.cast(classes, tf.int32)
        n_cols = preds.shape[1]

        splitted = tf.strings.split(inputs, sep=" ")
        n_tokens = tf.map_fn(fn=lambda t: tf.repeat(tf.size(t), n_cols), elems=splitted, fn_output_signature=tf.int32)
        stacked = tf.stack([classes, n_tokens], axis=1)

        def get_classes(tensor):
            """
            returns class labels
            """
            n_tokens = tensor[1, 0]
            cl_int = tf.slice(tensor[0], [0], [n_tokens])
            res = tf.map_fn(fn=lambda t: tf.convert_to_tensor(
                self.labels)[tensor],
                elems=cl_int, fn_output_signature=tf.string)
            return tf.strings.reduce_join(res, separator=" ")
        return tf.map_fn(fn=get_classes, elems=stacked,
                         fn_output_signature=tf.string)

    def get_config(self):
        config = super().get_config()
        config.update({"labels": self.labels})
        return config

    @classmethod
    def from_config(cls, config):
        return cls(**config)


def train_model(config: NERConfigReader) -> None:
    """
    training function which prints classification summary as as result
    Args:
        config: Configuration object containing parsed .json file parameters
    Return:
        None
    """
    X, y = [], []
    if config.dataset_name == "kaggle_ner":
        dataset = KaggleDataset(config.dataset_url)
        X, y_tagged = dataset.get_set()
    tags = [item for sublist in y_tagged for item in sublist]
    unique_labels = list(set(tags))
    tags2index = {t: i + 1 for i, t in enumerate(unique_labels)}
    tags2index["pad"] = 0
    n_tags = len(tags2index)
    y = [[tags2index[w] for w in s] for s in y_tagged]
    if config.experimental_mode:
        ind = np.random.randint(0, len(X), 500)
        X = [X[i] for i in ind]
        y = [y[i] for i in ind]
    X = [" ".join(x) for x in X]
    labels = tf.keras.preprocessing.sequence.pad_sequences(
        maxlen=config.max_sequence_length, sequences=y,
        padding="post", value=tags2index["pad"])
    d_size = len(X)
    valid_size = (int)(d_size * config.validation_split)
    dataset = tf.data.Dataset.from_tensor_slices((X, labels))
    # check dataset specifications
    # print("dataset spec {}".format(dataset.element_spec))
    valid_ds = dataset.take(valid_size).shuffle(10000).batch(64).prefetch(tf.data.AUTOTUNE)
    train_ds = dataset.skip(valid_size).shuffle(10000).batch(64).prefetch(tf.data.AUTOTUNE)

    # build and compile model
    tokenizer = tf.keras.layers.experimental.preprocessing.TextVectorization(
        # ngrams=3,
        max_tokens=config.vocab_size,
        output_sequence_length=config.max_sequence_length,
        output_mode='int')
    tokenizer.adapt(dataset.map(lambda text, label: text))
    """
    # visualize tokenizer transformation

    vocab = np.array(tokenizer.get_vocabulary())
    for example, label in dataset.take(1):
        encoded_example = tokenizer(example)[:3].numpy()
        for n in range(3):
            print("Original: ", example[n].numpy())
            print("Round-trip: ", " ".join(vocab[encoded_example[n]]))
            print()
    """

    input_layer = tf.keras.Input(shape=(1,), dtype=tf.string, name='input')
    x = tokenizer(input_layer)
    x = tf.keras.layers.Embedding(input_dim=len(tokenizer.get_vocabulary()),
                                  output_dim=config.embedding_dimension,
                                  input_length=config.max_sequence_length, trainable=True)(x)
    x = tf.keras.layers.Bidirectional(tf.keras.layers.LSTM(
        units=50, return_sequences=True, recurrent_dropout=0.2, dropout=0.2))(x)
    x_rnn = tf.keras.layers.Bidirectional(tf.keras.layers.LSTM(
        units=50, return_sequences=True, recurrent_dropout=0.2, dropout=0.2))(x)
    x = tf.keras.layers.add([x, x_rnn])  # residual connection to the first biLSTM
    x = tf.keras.layers.TimeDistributed(tf.keras.layers.Dense(n_tags, activation='softmax'))(x)

    model = tf.keras.Model(inputs=input_layer, outputs=x)
    print(model.summary())
    model.compile(loss=tf.keras.losses.SparseCategoricalCrossentropy(from_logits=False),
                  optimizer='adam', metrics=["accuracy"])
    history = model.fit(train_ds, validation_data=valid_ds, epochs=config.n_iter)
    save_perf(config.model_name, history.history)

    # export model
    reverted = {v: k for k, v in tags2index.items()}
    reverted = dict(sorted(reverted.items()))
    labels = tf.convert_to_tensor(list(reverted.values()))
    preds = model.get_layer("time_distributed").output
    x = PostProcessor(list(reverted.values()))([preds, input_layer])
    export_model = tf.keras.Model(inputs=input_layer, outputs=x)
    export_model.compile(loss=tf.keras.losses.SparseCategoricalCrossentropy(from_logits=False),
                         optimizer='adam', metrics=["accuracy"])
    # test the model
    # x = tf.convert_to_tensor(["we are outside", "i go to paris"])
    # print(export_model(x))
    tf.keras.models.save_model(export_model, os.path.join(MODELS_DIR, config.model_name))


def main():
    """main function"""
    train_config = NERConfigReader(NER_CONFIG_FILE)
    train_model(train_config)


if __name__ == '__main__':
    main()
