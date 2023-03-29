# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------
import logging
import tempfile
from copy import deepcopy
from pathlib import Path
from shutil import copyfile
from typing import Any, Callable, Dict, Union

from onnxruntime.quantization import QuantFormat, QuantType, quantize_dynamic, quantize_static
from onnxruntime.quantization.calibrate import CalibrationMethod
from onnxruntime.quantization.preprocess import quant_pre_process

from olive.model import ONNXModel
from olive.passes import Pass
from olive.passes.pass_config import PassConfigParam
from olive.strategy.search_parameter import Boolean, Categorical, Conditional

logger = logging.getLogger(__name__)

# common config for both static and dynamic quantization
_onnx_quantization_config = {
    "weight_type": PassConfigParam(
        type_=str,
        default="QInt8",
        default_search=Categorical(["QInt8", "QUInt8"]),
        description="""
            Data type for quantizing weights which is used both in dynamic
            and static quantization. 'QInt8' for signed 8-bit integer,
            'QUInt8' for unsigned 8-bit integer.
        """,
    ),
    "op_types_to_quantize": PassConfigParam(
        type_=list,
        default=None,
        description="""
            List of operator types to quantize. If None, all quantizable.
        """,
    ),
    "nodes_to_quantize": PassConfigParam(
        type_=list,
        default=None,
        description="""
            List of node names to quantize. If None, all quantizable.
        """,
    ),
    "nodes_to_exclude": PassConfigParam(
        type_=list,
        default=None,
        description="""
            List of node names to exclude from quantization. If None, all quantizable.
        """,
    ),
    "per_channel": PassConfigParam(
        type_=bool,
        default=False,
        default_search=Boolean(),
        description="""
            Quantize weights per channel.
            Tips: When to use reduce_range and per-channel quantization:
            https://onnxruntime.ai/docs/performance/quantization.html#when-to-use-reduce-range-and-per-channel-quantization
        """,
    ),
    "reduce_range": PassConfigParam(
        type_=bool,
        default=False,
        default_search=Boolean(),
        description="""
            Quantize weights with 7-bits. It may improve the accuracy for
            some models running on non-VNNI machine, especially for per-channel mode.
            Tips: When to use reduce_range and per-channel quantization:
            https://onnxruntime.ai/docs/performance/quantization.html#when-to-use-reduce-range-and-per-channel-quantization
        """,
    ),
    "optimize_model": PassConfigParam(
        type_=bool,
        default=False,
        default_search=Boolean(),
        description="""
            Deprecating Soon in ONNX! Optimize model before quantization. NOT recommended, optimization will
            change the computation graph, making debugging of quantization loss difficult.
        """,
    ),
    # TODO: enable search if we support onnx external data format
    "use_external_data_format": PassConfigParam(
        type_=bool,
        default=False,
        description="""
            option used for large size (>2GB) model. Set to False by default.
        """,
    ),
    "quant_preprocess": PassConfigParam(
        type_=bool,
        default=True,
        default_search=Boolean(),
        description="""
            Shape inference and model optimization, in preparation for quantization.
            https://onnxruntime.ai/docs/performance/quantization.html#pre-processing
        """,
    ),
}

# static quantization specific config
_static_dataloader_config = {
    "data_dir": PassConfigParam(
        type_=Union[Path, str],
        is_path=True,
        description="""
            Path to the directory containing the dataset.
            For local data, it is required if quant_mode is 'static'.
        """,
    ),
    "batch_size": PassConfigParam(
        type_=int,
        default=1,
        description="""
            Batch size for calibration, required if quant_mode is 'static'.
        """,
    ),
    "dataloader_func": PassConfigParam(
        type_=Union[Callable, str],
        required=True,
        is_object=True,
        description="""
            Function/function name to generate dataloader for calibration,
            required if quant_mode is 'static'
        """,
    ),
}

_static_optional_config = {
    "calibrate_method": PassConfigParam(
        type_=str,
        default="MinMax",
        default_search=Categorical(["MinMax", "Entropy", "Percentile"]),
        description="""
            Current calibration methods supported are MinMax and Entropy,
            Please use CalibrationMethod.MinMax or CalibrationMethod.Entropy as options.
        """,
    ),
    "quant_format": PassConfigParam(
        type_=str,
        default="QDQ",
        default_search=Categorical(["QOperator", "QDQ"]),
        description="""
            QOperator format quantizes the model with quantized operators directly.
            QDQ format quantize the model by inserting QuantizeLinear/DeQuantizeLinear on the tensor.
        """,
    ),
    "activation_type": PassConfigParam(
        type_=str,
        default="QInt8",
        default_search=Conditional(
            parents=("quant_format",),
            support={("QDQ",): Categorical(["QInt8", "QUInt8"]), ("QOperator",): Categorical(["QInt8"])},
        ),
        description="""
            Quantization data type of activation. Please refer to
            https://onnxruntime.ai/docs/performance/quantization.html for more details on data type selection
        """,
    ),
}


class OnnxQuantization(Pass):
    """
    Quantize ONNX model with onnxruntime where we can search for
    best parameters for static/dynamic quantization at same time.
    """

    _requires_user_script = True

    def _initialize(self):
        super()._initialize()
        self.tmp_dir = tempfile.TemporaryDirectory()

    @staticmethod
    def _default_config() -> Dict[str, Any]:
        config = {
            "quant_mode": PassConfigParam(
                type_=str,
                default="static",
                default_search=Categorical(["dynamic", "static"]),
                description="""
                    Onnx Quantization mode. 'dynamic' for dynamic quantization,
                    'static' for static quantization.
                """,
            )
        }

        # common quantization config
        config.update(deepcopy(_onnx_quantization_config))

        # static quantization config
        config.update(deepcopy(_static_dataloader_config))
        static_optional_config = deepcopy(_static_optional_config)
        for _, value in static_optional_config.items():
            if isinstance(value.default_search, Categorical):
                value.default_search = Conditional(
                    parents=("quant_mode",),
                    support={("static",): value.default_search},
                    default=Categorical(["Invalid"]),
                )
            elif isinstance(value.default_search, Conditional):
                value.default_search = Conditional(
                    parents=("quant_mode",) + value.default_search.parents,
                    support={
                        ("static",) + key: value.default_search.support[key] for key in value.default_search.support
                    },
                    default=Categorical(["Invalid"]),
                )
        config.update(static_optional_config)
        return config

    def validate_search_point(self, search_point: Dict[str, Any]) -> bool:
        config = self.config_at_search_point(search_point)
        if config["quant_mode"] == "static":
            if (
                config["weight_type"] == "QInt8"
                and config["activation_type"] == "QInt8"
                and config["quant_format"] == "QOperator"
            ):
                logger.info("QOperator is not supported for QInt8 activation and weight.")
                return False
            if config["weight_type"] != config["activation_type"]:
                logger.info("Weight type and activation type must be the same.")
                return False
        return True

    def _run_for_config(self, model: ONNXModel, config: Dict[str, Any], output_model_path: str) -> ONNXModel:
        # start with a copy of the config
        run_config = deepcopy(config)
        is_static = run_config["quant_mode"] == "static"

        # add onnx extension if not present
        if Path(output_model_path).suffix != ".onnx":
            output_model_path += ".onnx"

        # preprocess the model
        preprocessed_temp_model_path = Path(self.tmp_dir.name) / f"{Path(model.model_path).stem}_preprocessed.onnx"
        if run_config["quant_preprocess"]:
            if not preprocessed_temp_model_path.exists():
                # overwrite the model path with the preprocessed model path
                logger.info("Preprocessing model for quantization")
                model = self._quant_preprocess(model, preprocessed_temp_model_path)
            else:
                logger.info("Already processed model for quantization, skipping preprocessing")
                model = ONNXModel(preprocessed_temp_model_path)

        # keys not needed for quantization
        to_delete = ["quant_mode", "script_dir", "user_script", "quant_preprocess"]

        # update string values to enum values
        if is_static:
            to_delete += list(_static_dataloader_config.keys())
            run_config.update(
                {
                    "calibrate_method": CalibrationMethod[run_config["calibrate_method"]],
                    "quant_format": QuantFormat[run_config["quant_format"]],
                    "activation_type": QuantType[run_config["activation_type"]],
                    "weight_type": QuantType[run_config["weight_type"]],
                    "extra_options": {},
                }
            )
        else:
            to_delete += list(_static_dataloader_config.keys())
            to_delete += list(_static_optional_config.keys())
            run_config.update(
                {
                    "weight_type": QuantType[run_config["weight_type"]],
                    "extra_options": {},
                }
            )
        # remove keys not needed for quantization
        for key in to_delete:
            if key in run_config:
                del run_config[key]
        # add extra options to the extra options dictionary
        for key, value in config.items():
            if key.startswith("eo_"):
                run_config["extra_options"][key] = value
                del run_config[key]

        if is_static:
            # get the dataloader
            dataloader = self._user_module_loader.call_object(
                self._fixed_params["dataloader_func"], self._fixed_params["data_dir"], self._fixed_params["batch_size"]
            )
            quantize_static(
                model_input=model.model_path,
                model_output=output_model_path,
                calibration_data_reader=dataloader,
                **run_config,
            )
        else:
            quantize_dynamic(model_input=model.model_path, model_output=output_model_path, **run_config)

        return ONNXModel(output_model_path, model.name)

    def _quant_preprocess(self, model: ONNXModel, output_model_path: str) -> ONNXModel:
        try:
            quant_pre_process(input_model_path=model.model_path, output_model_path=output_model_path, auto_merge=True)
        except Exception as e:
            logger.warning(f"failed to run quantization preprocessing with error of {e}")
            copyfile(model.model_path, output_model_path)

        return ONNXModel(output_model_path)


class OnnxDynamicQuantization(OnnxQuantization):
    """ONNX Dynamic Quantization Pass"""

    _requires_user_script = False

    @staticmethod
    def _default_config() -> Dict[str, Any]:
        config = {"quant_mode": PassConfigParam(type_=str, default="dynamic", description="dynamic quantization mode")}
        # common quantization config
        config.update(deepcopy(_onnx_quantization_config))
        return config


class OnnxStaticQuantization(OnnxQuantization):
    """ONNX Static Quantization Pass"""

    _requires_user_script = True

    @staticmethod
    def _default_config() -> Dict[str, Any]:
        config = {"quant_mode": PassConfigParam(type_=str, default="static", description="static quantization mode")}
        # common quantization config
        config.update(deepcopy(_onnx_quantization_config))
        # static quantization specific config
        config.update(deepcopy(_static_dataloader_config))
        config.update(deepcopy(_static_optional_config))
        return config