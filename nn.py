import time
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler, MinMaxScaler
from sklearn.metrics import mean_squared_error, mean_absolute_error, mean_absolute_percentage_error, r2_score
from sklearn.linear_model import LinearRegression
from sklearn.svm import SVR
from sklearn.ensemble import BaggingRegressor
import numpy as np
from matplotlib import pyplot as plt
from sklearn.model_selection import cross_val_score

import tensorflow as tf
from keras import backend as K
from keras.models import Model,Sequential,load_model 
from keras.layers import Input, Dense, concatenate, Flatten, Reshape, Lambda, Embedding
from tensorflow.keras.optimizers import Adam
from tensorboard import main as tb
#from tensorflow.keras.models import load_model

LABEL_DATA_COL_INDEX = [-3, -2, -1]

# Mean Absolute Percentage Error
# https://brunch.co.kr/@chris-song/34
def MAPE(y_test, y_hat):
    return np.mean(np.abs((y_test - y_hat) / y_test)) * 100

def RMSE(y_test, y_hat):
    return K.sqrt(K.mean(K.square(y_test - y_hat),axis=-1))


def ml(trained=False):
    # refer to sample_dataset_columns.txt for column numbers.
    df = pd.read_csv("sample_dataset_5000.csv", header=None)
    min_features = [0, 2, 7, 8, 18, 41, 50, 56, 58, 65]
    df = df.iloc[:, min_features + LABEL_DATA_COL_INDEX]

    #print(df.shape)
    #print(df.describe())
    X = df.iloc[:, :-len(LABEL_DATA_COL_INDEX)].values

    # Y: label data (single value). uncomment the corresponding line for label you want to predict
    #y = df.iloc[:, -3:-2].values      # label: total migration time (MT)
    y = df.iloc[:, -2:-1].values
    #y = df.iloc[:, -1:].values        # label: vm downtime
    #print(y)

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=1)

    scaler = StandardScaler()
    scaler.fit(X_train)
    X_train_scaled = scaler.transform(X_train)
    X_test_scaled = scaler.transform(X_test)

    # Flatten the y-values into 1d array.
    y_train = y_train.ravel()
    y_test = y_test.ravel()

    X_size = X_train_scaled.shape[-1]
    Y_size = y_train.shape[-1]

    # print("MAE:", mean_absolute_error(y_test, y_hat))
    # print("RMSE: ", np.sqrt(mean_squared_error(y_test, y_hat)))
    # # print("MAPE:", MAPE(y_test, y_hat))
    # print("MAPE: ", 100 * mean_absolute_percentage_error(y_test, y_hat))
    # print("R-squared: ", r2_score(y_test, y_hat))
    # print("Learning time (s): ", f'{learning_time:.5f}')
    # print("Prediction time (s): ", f'{prediction_time:.5f}')


    X_in = Input(shape=(X_size))

    layer1 = Dense(300)(X_in)
    layer2 = Dense(300)(layer1)
    layer3 = Dense(30)(layer2)
    output = Dense(1)(layer3)


    #mae = tf.keras.metrics.MeanAbsoluteError()
    #mape = tf.keras.metrics.MeanAbsolutePercentageError()


    l_size = 0.0001
    e_size = 1000
    b_size = 10000

    model = Model(inputs=[X_in],outputs=output)
    optimizer = Adam(lr=l_size)
    model.compile(optimizer=optimizer,loss=tf.keras.losses.MeanAbsoluteError(),metrics=[tf.keras.metrics.MeanAbsolutePercentageError()])
    #model.summary()

    if trained == False:
        model.fit([X_train_scaled],y_train,batch_size=b_size,validation_data=([X_test_scaled],y_test),epochs=e_size)
        model.save('preidiction_model.h5')
        
    else :
        model = load_model('preidiction_model.h5')


    loss, accuracy = model.evaluate([X_test_scaled],y_test,batch_size=10000)

    return (100.0-float(accuracy))
    


