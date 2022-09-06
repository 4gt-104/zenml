#  Copyright (c) ZenML GmbH 2022. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at:
#
#       https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
#  or implied. See the License for the specific language governing
#  permissions and limitations under the License.

import pandas as pd
from pyspark.sql import DataFrame

from zenml.repository import Repository
from zenml.stack.stack import Stack
from zenml.steps import step

active_stack = Stack.from_model(Repository().active_stack)
step_operator = active_stack.step_operator


@step(custom_step_operator=step_operator.name)
def statistics_step(dataset: DataFrame) -> pd.DataFrame:
    return dataset.describe().toPandas()  # noqa
