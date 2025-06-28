import boto3
import json
import os

ssm = boto3.client("ssm")
envs = ["prod"]
ssm_paths = [f"/{env}/voicenest_serverless/" for env in envs]

def get_parameters(path):
    paginator = ssm.get_paginator("get_parameters_by_path")
    result = {}
    for page in paginator.paginate(Path=path, Recursive=True, WithDecryption=True):
        for param in page["Parameters"]:
            key = param["Name"].split("/")[-1].upper()
            result[f"{path.split('/')[1].upper()}_{key}"] = param["Value"]
    return result

final_vars = {}
for p in ssm_paths:
    final_vars.update(get_parameters(p))

output = {
    "variable": {
        "lambda_env_vars": {
            "default": final_vars
        }
    }
}

with open("./infra/lambda_env_vars.tf.json", "w") as f:
    json.dump(output, f, indent=2)
