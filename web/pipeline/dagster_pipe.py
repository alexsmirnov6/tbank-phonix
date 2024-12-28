import gdown
import os
import zipfile
import warnings

warnings.filterwarnings("ignore")

from comet_ml import Experiment, init
from comet_ml.integration.pytorch import log_model, watch
from dagster import job, op, Out, In

from pipeline.utils import seed_everything, empty_cache

from pipeline.train_functions import *
from pipeline.preproccess_functions import *

base_dir = "."
weights_folder = f"{base_dir}/weights"
path_to_data_folder = f'{base_dir}/data_processed/words'

def get_order(x):
    return 123

@op
def download_dataset():
    ORIGINAL_DIR = os.getcwd()
    os.chdir(ORIGINAL_DIR)

    os.chdir("runs")

    data_link = "https://drive.google.com/uc?id=16NmrEiqS5Up_jcwbGNAC62yAfR3ZkJrs"
    run_name = "new_run_6"

    archive_name = gdown.download(data_link)

    if os.path.exists(run_name):
        print("ERROR!!!!")
    else:
        extract_folder = run_name
        os.makedirs(extract_folder, exist_ok=True)
        # Открываем и распаковываем архив
        with zipfile.ZipFile(archive_name, 'r') as zip_ref:
            zip_ref.extractall(extract_folder)

    os.chdir(run_name)
    return get_order(1)

@op
def prepare_env(order_var):
    disorders_letters = dict()
    disorders_letters[0] = []
    disorders_letters[1] = ["р"]
    disorders_letters[2] = ["г"]
    disorders_letters[3] = []

    target_letters = []
    for letters in disorders_letters.values():
        target_letters.extend(letters)

    os.makedirs("data_processed", exist_ok=True)

    with open("data_processed/target_letters.pkl", "wb") as f:
        pickle.dump(target_letters, f)

    with open("data_processed/disorders_letters.pkl", "wb") as f:
        pickle.dump(disorders_letters, f)

    os.makedirs("data_wav", exist_ok=True)
    return target_letters


@op(out={"train": Out(), "letters": Out()})
def process_train_df(order_var):
    train = pd.read_csv("data_train.csv", header=None)
    train[1].value_counts(normalize=True)
    target_samplerate = 16000

    convert_folder_to_wav('train', target_samplerate=target_samplerate)
    # convert_folder_to_wav('test', target_name='test', target_samplerate=target_samplerate)

    letters = set()  # Какие буквы оставить (удаляем знаки препинания/цифры/английские символы из транскрибации)
    for i in range(33):
        letters.add(chr(ord('а') + i))
    print(letters)

    # return train
    return train, letters


@op
def transcribe_for_pretrain(order_var):
    # !mkdir data_whisper
    os.makedirs("data_whisper", exist_ok=True)
    res_whisper_train = process_audio_folder_by_whisper(os.path.join("data_mp3", "train"))
    # res_whisper_test = process_audio_folder_by_whisper(os.path.join("data_mp3", "test"))

    result_whisper = res_whisper_train.copy()
    with open("data_whisper/result_whisper.pkl", "wb") as f:
        pickle.dump(result_whisper, f)

    # оставлено так на случай если надо несколько .pkl файлов использовать
    whisper_res = {}
    for root, dirs, files in os.walk('data_whisper'):
        for file in files:
            path = os.path.join(root, file)

            with open(path, 'rb') as f:
                whisper_res_one_part = pickle.load(f)

            for k, v in whisper_res_one_part.items():
                v['data_type'] = 'first_stage'
                whisper_res[k] = v
    return whisper_res


@op
def get_final_df(order_var):
    train1 = pd.read_csv("data_train.csv", header=None)
    train1['data_type'] = 'final_train'

    # test1 = pd.read_csv("data_test.csv", header=None)
    # test1['data_type'] = 'final_test'

    # y = pd.concat([train1, test1], axis=0, ignore_index=True)
    y = pd.concat([train1], axis=0, ignore_index=True)
    y = process_y(y)
    y.head()
    return y


@op
def get_val_and_train_files(y):
    _, val_files = train_test_split(list(y[y['data_type'] == 'final_train'].index),
                                    test_size=0.2, random_state=42,
                                    stratify=y.loc[y['data_type'] == 'final_train', 'target'])

    train_files = [file for file in y.index if file not in val_files]

    # !mkdir "data_processed/words"
    os.makedirs("data_processed/words", exist_ok=True)
    with open('data_processed/words/val_files.pkl', 'wb') as f:
        pickle.dump(val_files, f)

    return {"train_files": train_files, "val_files": val_files}


@op
def load_train_val(base_dir, path_to_data_folder):
    train = pd.read_parquet(f'{path_to_data_folder}/train.parquet')
    val = pd.read_parquet(f'{path_to_data_folder}/val.parquet')
    # test = pd.read_parquet(f'{path_to_data_folder}/test.parquet')

    return {"train": train, "val": val}

class Cfg:
    model_type = "wav2vec"
    model_name = "jonatasgrosman/wav2vec2-large-xlsr-53-russian"

    batch_size = 32 if torch.cuda.is_available() else 2

    max_length = 16000 * 5

    letter_count_weights = {}
    letters_num_classes = {}

    label_smoothing_pretrain = 0.0
    label_smoothing_train = 0.0

    linear_probing_frac = 0.1  # Часть первой эпохи, в течение которой все веса, кроме головы, замораживаются
    zero_epoch_evaluation_frac = 0.1  # На какой части данных оценивать модель перед обучением (в начале 0-й эпохи)

    head_dim = 256
    dropout = 0.25
    lr_pretrain = 1e-4
    lr_train = 1e-4
    num_epochs_pretrain = 10
    num_epochs_train = 10
    metric_computation_times_per_epoch_train = 4
    metric_computation_times_per_epoch_val = 1

    early_stopping_pretrain = 1
    early_stopping_train = 3


@op
def get_cfg(target_letters, train, weights_folder):
    cfg = Cfg()
    cfg.target_letters = target_letters
    cfg.disorders_class_weights = torch.tensor(compute_class_weights_sqrt(train['label'].dropna()),
                                               device=device, dtype=torch.float32)
    cfg.weights_folder = weights_folder

    return cfg


@op
def run_whisper(y, train_files_val_files, whisper_res, letters, target_letters):
    # letters = processed_df_result["letters"]
    train_files = train_files_val_files["train_files"]
    val_files = train_files_val_files["val_files"]

    # y_train, y_val = y.loc[train_files], y.loc[val_files]
    whisper_res_train = {file: whisper_res[file] for file in train_files}
    whisper_res_val = {file: whisper_res[file] for file in val_files}
    train, train_arrays = process_whisper_res(whisper_res_train, y, target_letters=target_letters, letters=letters)
    assert len(train) == len(train_arrays)

    train.to_parquet('data_processed/words/train.parquet')

    with open('data_processed/words/train_arrays.pkl', 'wb') as f:
        pickle.dump(train_arrays, f)
    del train_arrays

    print(2)

    val, val_arrays = process_whisper_res(whisper_res_val, y, target_letters=target_letters, letters=letters)
    val.to_parquet('data_processed/words/val.parquet')

    with open('data_processed/words/val_arrays.pkl', 'wb') as f:
        pickle.dump(val_arrays, f)
    del val_arrays

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    return device


@op(out={"cgf": Out(), "train_arrays": Out(), "val_arrays": Out(), "train": Out(), "val": Out(),})
def prepare_data(device):
    global base_dir, path_to_data_folder, weights_folder
    train_val_res = load_train_val(base_dir, path_to_data_folder)
    train, val = train_val_res["train"], train_val_res["val"]
    print(3)

    ###########
    os.makedirs(weights_folder, exist_ok=True)
    with open(f"{base_dir}/data_processed/target_letters.pkl", "rb") as f:
        target_letters = pickle.load(f)

    with open(f'{path_to_data_folder}/train_arrays.pkl', 'rb') as f1, open(f'{path_to_data_folder}/val_arrays.pkl',
                                                                           'rb') as f2:
        train_arrays = pickle.load(f1)
        val_arrays = pickle.load(f2)
    ############

    print(4)

    rare_borders = get_rare_classes(train, target_letters=target_letters)

    letter_count_weights = {}
    letters_num_classes = {}

    for letter in target_letters:
        train[f"{letter}_count"] = train[f"{letter}_count"].apply(lambda x: min(x, rare_borders[letter]))
        val[f"{letter}_count"] = val[f"{letter}_count"].apply(lambda x: min(x, rare_borders[letter]))
        # test[f"{letter}_count"] = test[f"{letter}_count"].apply(lambda x: min(x, rare_borders[letter]))

        letter_count_weights[letter] = torch.tensor(compute_class_weights_sqrt(train[f"{letter}_count"]),
                                                    device=device, dtype=torch.float32)
        letters_num_classes[letter] = train[f"{letter}_count"].nunique()

    cfg = get_cfg(target_letters, train, weights_folder)
    cfg.letter_count_weights = letter_count_weights
    cfg.letters_num_classes = letters_num_classes
    cfg.save_model_name = f'{cfg.model_type}'
    return cfg, train_arrays, val_arrays,  train, val


@op
def get_dataloader_and_experiment(cfg, train, val, train_arrays, val_arrays, target_letters):

    dataset_train = CustomDataset(train, train_arrays, target_letters=target_letters)
    dataset_val = CustomDataset(val, val_arrays, target_letters=target_letters)
    data_collator = DataCollator(cfg=cfg)
    dataloader_train = DataLoader(dataset_train, batch_size=cfg.batch_size, collate_fn=data_collator, shuffle=True)
    dataloader_val = DataLoader(dataset_val, batch_size=cfg.batch_size * 2, collate_fn=data_collator, shuffle=False)

    # experiment = Experiment(
    #     api_key="_rpI0PuxxYkKMtiy42g1oIfLI1",
    #     project_name="aiijc-final-pretrain",
    #     workspace="ugryumnik"
    # )

    return {"dataloader_train": dataloader_train,
            "dataloader_val": dataloader_val,
            "experiment": None}

@op(out={"experiment": Out(), "dataloader_train": Out(), "dataloader_val": Out()})

def get_dataloaders(cfg, train, val, train_arays, val_arrays, target_letters):
    train.fillna({"label": -100}, inplace=True)
    dataloaders_and_experiment = get_dataloader_and_experiment(cfg, train, val, train_arays, val_arrays,
                                                               target_letters)

    experiment = dataloaders_and_experiment["experiment"]
    dataloader_train, dataloader_val = (dataloaders_and_experiment["dataloader_train"],
                                        dataloaders_and_experiment["dataloader_val"])

    return experiment, dataloader_train, dataloader_val

@op
def train_model_main(cfg, dataloader_train, dataloader_val, experiment):
    # experiment.log_parameters(dict(vars(cfg)))
    empty_cache()
    seed_everything(42)
    model = DisordersDetector(cfg=cfg, stage='pretrain')
    model.to(device)
    if cfg.model_type == "wav2vec":
        model.freeze_feature_extractor()

    train_model(model, cfg, dataloader_train, dataloader_val, experiment, stage='pretrain')


@job
def full_pipeline():
    order_var = download_dataset()
    target_letters = prepare_env(order_var)

    train, letters = process_train_df(target_letters)
    # processed_df_result = {"train": train, "letters": letters}
    # letters, train = processed_df_result["letters"], processed_df_result["train"]

    whisper_res = transcribe_for_pretrain(train)
    y = get_final_df(whisper_res)

    train_files_val_files = get_val_and_train_files(y)
    # train_files, val_files = train_val["train_files"], train_val["val_files"]

    print(1)
    device = run_whisper(y, train_files_val_files, whisper_res, letters, target_letters)

    cfg, train_arrays, val_arrays, train, val = prepare_data(device)  # base_dir, path_to_data_folder, weights_folder)

    # train_arrays = res["train_arrays"]
    # val_arrays = res["val_arrays"]
    # train = res["train"]
    # val = res["val"]
    print(5)

    experiment, dataloader_train, dataloader_val = get_dataloaders(cfg, train, val, train_arrays, val_arrays, target_letters)
    print(6)

    train_model_main(cfg, dataloader_train, dataloader_val, experiment)
