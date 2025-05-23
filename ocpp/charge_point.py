import asyncio
import inspect
import logging
import re
import time
import uuid
import json
from dataclasses import Field, asdict, is_dataclass
from typing import Any, Dict, List, Union, get_args, get_origin

from ocpp.exceptions import NotImplementedError, NotSupportedError, OCPPError
from ocpp.messages import Call, MessageType, unpack, validate_payload
from ocpp.routing import create_route_map

LOGGER = logging.getLogger("ocpp")


def camel_to_snake_case(data):
    """
    Convert all keys of all dictionaries inside the given argument from
    camelCase to snake_case.

    Inspired by: https://stackoverflow.com/a/1176023/1073222

    """
    if isinstance(data, dict):
        snake_case_dict = {}
        for key, value in data.items():
            key = key.replace("ocppCSMSURL", "ocpp_csms_url")
            key = key.replace("V2X", "_v2x").replace("V2G", "_v2g")
            s1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", key)
            key = re.sub("([a-z0-9])([A-Z])(?=\\S)", r"\1_\2", s1).lower()

            snake_case_dict[key] = camel_to_snake_case(value)

        return snake_case_dict

    if isinstance(data, list):
        snake_case_list = []
        for value in data:
            snake_case_list.append(camel_to_snake_case(value))

        return snake_case_list

    return data


def snake_to_camel_case(data):
    """
    Convert all keys of all dictionaries inside given argument from
    snake_case to camelCase.

    Inspired by: https://stackoverflow.com/a/19053800/1073222
    """
    if isinstance(data, dict):
        camel_case_dict = {}
        for key, value in data.items():
            key = key.replace("soc", "SoC")
            key = key.replace("_v2x", "V2X")
            # The spec uses inconsent casing for "csms" and "url".
            # E.g. "OcppCsmsUrl" vs "ResponderURL" and "CSMSRootCertificate"
            key = key.replace("ocpp_csms_url", "ocppCsmsUrl")
            key = key.replace("csms", "CSMS")
            key = key.replace("_url", "URL")
            key = key.replace("soc", "SoC").replace("_SoCket", "Socket")
            key = key.replace("_v2x", "V2X")
            key = key.replace("soc_limit_reached", "SOCLimitReached")
            key = key.replace("_v2x", "V2X").replace("_v2g", "V2G")
            components = key.split("_")
            key = components[0] + "".join(x[:1].upper() + x[1:] for x in components[1:])
            camel_case_dict[key] = snake_to_camel_case(value)

        return camel_case_dict

    if isinstance(data, list):
        camel_case_list = []
        for value in data:
            camel_case_list.append(snake_to_camel_case(value))

        return camel_case_list

    return data


def _is_dataclass_instance(input: Any) -> bool:
    """Verify if given `input` is a dataclass."""
    return is_dataclass(input) and not isinstance(input, type)


def _is_optional_field(field: Field) -> bool:
    """Verify if given `field` allows `None` as value.

    The fields `schema` and `host` on the following class would return `False`.
    While the fields `post` and `query` return `True`.

        @dataclass
        class URL:
            schema: str,
            host: str,
            post: Optional[str],
            query: Union[None, str]

    """
    return get_origin(field.type) is Union and type(None) in get_args(field.type)


def serialize_as_dict(dataclass):
    """Serialize the given `dataclass` as a `dict` recursively.

    @dataclass
    class StatusInfoType:
        reason_code: str
        additional_info: Optional[str] = None

    with_additional_info = StatusInfoType(
        reason="Unknown",
        additional_info="More details"
    )

    assert serialize_as_dict(with_additional_info) == {
        'reason': 'Unknown',
        'additional_info': 'More details',
    }

    without_additional_info = StatusInfoType(reason="Unknown")

    assert serialize_as_dict(with_additional_info) == {
        'reason': 'Unknown',
        'additional_info': None,
    }

    """
    serialized = asdict(dataclass)

    for field in dataclass.__dataclass_fields__.values():
        value = getattr(dataclass, field.name)
        if _is_dataclass_instance(value):
            serialized[field.name] = serialize_as_dict(value)
            continue

        if isinstance(value, list):
            serialized[field.name] = []
            for item in value:
                if _is_dataclass_instance(item):
                    serialized[field.name].append(serialize_as_dict(item))
                else:
                    serialized[field.name].append(item)

    return serialized


def remove_nones(data: Union[List, Dict]) -> Union[List, Dict]:
    if isinstance(data, dict):
        return {k: remove_nones(v) for k, v in data.items() if v is not None}

    elif isinstance(data, list):
        return [remove_nones(v) for v in data if v is not None]

    return data


def _raise_key_error(action, version):
    """
    Checks whether a keyerror returned by _handle_call
    is supported by the OCPP version or is simply
    not implemented by the server/client and raises
    the appropriate error.
    """

    from ocpp.v16.enums import Action as v16_Action
    from ocpp.v201.enums import Action as v201_Action

    if version == "1.6":
        try:
            v16_Action(action)
            raise NotImplementedError(
                details={"cause": f"No handler for {action} registered."}
            )
        except ValueError:
            raise NotSupportedError(
                details={"cause": f"{action} not supported by OCPP{version}."}
            )
    elif version in ["2.0", "2.0.1"]:
        try:
            v201_Action(action)
            raise NotImplementedError(
                details={"cause": f"No handler for {action} registered."}
            )
        except ValueError:
            raise NotSupportedError(
                details={"cause": f"{action} not supported by OCPP{version}."}
            )

    return


class ChargePoint:
    """
    Base Element containing all the necessary OCPP1.6J messages for messages
    initiated and received by the Central System
    """

    def __init__(self, id, connection, response_timeout=30, cursor=None, logger=LOGGER):
        """

        Args:

            charger_id (str): ID of the charger.
            connection: Connection to CP.
            response_timeout (int): When no response on a request is received
                within this interval, a asyncio.TimeoutError is raised.
            logger: Optional Logger instance used for logging.
                By default, the 'ocpp' logger is used.

        """
        self.id = id
        self._cursor = cursor

        # The maximum time in seconds it may take for a CP to respond to a
        # CALL. An asyncio.TimeoutError will be raised if this limit has been
        # exceeded.
        self._response_timeout = response_timeout

        # A connection to the client. Currently this is an instance of gh
        self._connection = connection

        # A dictionary that hooks for Actions. So if the CS receives a it will
        # look up the Action into this map and execute the corresponding hooks
        # if exists.
        self.route_map = create_route_map(self)

        self._call_lock = asyncio.Lock()

        # A queue used to pass CallResults and CallErrors from
        # the self.serve() task to the self.call() task.
        self._response_queue = asyncio.Queue()

        # Function used to generate unique ids for CALLs. By default
        # uuid.uuid4() is used, but it can be changed. This is meant primarily
        # for testing purposes to have predictable unique ids.
        self._unique_id_generator = uuid.uuid4

        # The logger used to log messages
        self.logger = logger

    async def start(self):
        while True:
            message = await self._connection.recv()
            self.logger.info("%s: receive message %s", self.id, message)

            await self.route_message(message)

    async def route_message(self, raw_msg):
        """
        Route a message received from a CP.

        If the message is a of type Call the corresponding hooks are executed.
        If the message is of type CallResult or CallError the message is passed
        to the call() function via the response_queue.
        """
        try:
            msg = unpack(raw_msg)
            self._cursor.execute(f"INSERT INTO carregadorlog (log, evento, idcarregador) VALUES ('{json.dumps(msg.payload)}', '{msg.action}', (SELECT idcarregador FROM carregador WHERE cdcarregador = '{self.id}'))")
        except OCPPError as e:
            self.logger.exception(
                "Unable to parse message: '%s', it doesn't seem "
                "to be valid OCPP: %s",
                raw_msg,
                e,
            )
            return

        if msg.message_type_id == MessageType.Call:
            try:
                await self._handle_call(msg)
            except OCPPError as error:
                self.logger.exception("Error while handling request '%s'", msg)
                response = msg.create_call_error(error).to_json()
                await self._send(response)

        elif msg.message_type_id in [MessageType.CallResult, MessageType.CallError]:
            self.logger.warning(f"Messagem teste: {msg}")
            self._response_queue.put_nowait(msg)

    async def _handle_call(self, msg):
        """
        Execute all hooks installed for based on the Action of the message.

        First the '_on_action' hook is executed and its response is returned to
        the client. If there is no '_on_action' hook for Action in the message
        a CallError with a NotImplementedError is returned. If the Action is
        not supported by the OCPP version a NotSupportedError is returned.

        Next the '_after_action' hook is executed.

        """
        try:
            handlers = self.route_map[msg.action]
        except KeyError:
            _raise_key_error(msg.action, self._ocpp_version)
            return

        if not handlers.get("_skip_schema_validation", False):
            await validate_payload(msg, self._ocpp_version)

        # OCPP uses camelCase for the keys in the payload. It's more pythonic
        # to use snake_case for keyword arguments. Therefore the keys must be
        # 'translated'. Some examples:
        #
        # * chargePointVendor becomes charge_point_vendor
        # * firmwareVersion becomes firmwareVersion
        snake_case_payload = camel_to_snake_case(msg.payload)

        try:
            handler = handlers["_on_action"]
        except KeyError:
            _raise_key_error(msg.action, self._ocpp_version)
        handler_signature = inspect.signature(handler)
        call_unique_id_required = "call_unique_id" in handler_signature.parameters
        try:
            # call_unique_id should be passed as kwarg only if is defined explicitly
            # in the handler signature
            if call_unique_id_required:
                response = handler(**snake_case_payload, call_unique_id=msg.unique_id)
            else:
                response = handler(**snake_case_payload)
            if inspect.isawaitable(response):
                response = await response
        except Exception as e:
            self.logger.exception("Error while handling request '%s'", msg)
            response = msg.create_call_error(e).to_json()
            await self._send(response)

            return

        temp_response_payload = serialize_as_dict(response)

        # Remove nones ensures that we strip out optional arguments
        # which were not set and have a default value of None
        response_payload = remove_nones(temp_response_payload)

        # The response payload must be 'translated' from snake_case to
        # camelCase. So:
        #
        # * charge_point_vendor becomes chargePointVendor
        # * firmware_version becomes firmwareVersion
        camel_case_payload = snake_to_camel_case(response_payload)

        response = msg.create_call_result(camel_case_payload)

        if not handlers.get("_skip_schema_validation", False):
            await validate_payload(response, self._ocpp_version)

        await self._send(response.to_json())

        try:
            handler = handlers["_after_action"]
            handler_signature = inspect.signature(handler)
            call_unique_id_required = "call_unique_id" in handler_signature.parameters
            # call_unique_id should be passed as kwarg only if is defined explicitly
            # in the handler signature
            if call_unique_id_required:
                response = handler(**snake_case_payload, call_unique_id=msg.unique_id)
            else:
                response = handler(**snake_case_payload)
            # Create task to avoid blocking when making a call inside the
            # after handler
            if inspect.isawaitable(response):
                asyncio.ensure_future(response)
        except KeyError:
            # '_on_after' hooks are not required. Therefore ignore exception
            # when no '_on_after' hook is installed.
            pass
        return response

    async def call(
        self, payload, suppress=True, unique_id=None, skip_schema_validation=False
    ):
        """
        Send Call message to client and return payload of response.

        The given payload is transformed into a Call object by looking at the
        type of the payload. A payload of type BootNotificationPayload will
        turn in a Call with Action BootNotification, a HeartbeatPayload will
        result in a Call with Action Heartbeat etc.

        A timeout is raised when no response has arrived before expiring of
        the configured timeout.

        When waiting for a response no other Call message can be send. So this
        function will wait before response arrives or response timeout has
        expired. This is in line the OCPP specification

        Suppress is used to maintain backwards compatibility. When set to True,
        if response is a CallError, then this call will be suppressed. When
        set to False, an exception will be raised for users to handle this
        CallError.

        Schema validation can be skipped for the request and the response
        for this call by setting `skip_schema_validation` to `True`.

        """
        camel_case_payload = snake_to_camel_case(serialize_as_dict(payload))

        unique_id = (
            unique_id if unique_id is not None else str(self._unique_id_generator())
        )

        action_name = payload.__class__.__name__

        call = Call(
            unique_id=unique_id,
            action=action_name,
            payload=remove_nones(camel_case_payload),
        )

        if not skip_schema_validation:
            await validate_payload(call, self._ocpp_version)

        # Use a lock to prevent make sure that only 1 message can be send at a
        # a time.
        async with self._call_lock:
            await self._send(call.to_json())
            try:
                response = await self._get_specific_response(
                    call.unique_id, self._response_timeout
                )
            except asyncio.TimeoutError:
                self.logger.error(
                    f"Waited {self._response_timeout}s for response on "
                    f"{call.to_json()}."
                )
                return
        if response == None:
            return

        if response.message_type_id == MessageType.CallError:
            self.logger.warning("Received a CALLError: %s'", response)
            if suppress:
                return
            raise response.to_exception()
        elif not skip_schema_validation:
            response.action = call.action
            await validate_payload(response, self._ocpp_version)

        snake_case_payload = camel_to_snake_case(response.payload)
        # Create the correct Payload instance based on the received payload. If
        # this method is called with a call.BootNotificationPayload, then it
        # will create a call_result.BootNotificationPayload. If this method is
        # called with a call.HeartbeatPayload, then it will create a
        # call_result.HeartbeatPayload etc.
        cls = getattr(self._call_result, payload.__class__.__name__)  # noqa
        return cls(**snake_case_payload)

    async def _get_specific_response(self, unique_id, timeout):
        """
        Return response with given unique ID or raise an asyncio.TimeoutError.
        """
        wait_until = time.time() + timeout
        try:
            # Wait for response of the Call message.
            response = await asyncio.wait_for(self._response_queue.get(), timeout)
            self.logger.error("Retorno response: %s", response)
        except asyncio.TimeoutError:
            self.logger.error("Tempo expirou: %s", unique_id)
            return

        if response.unique_id == unique_id:
            return response

        self.logger.error("Ignoring response with unknown unique id: %s", response)
        timeout_left = wait_until - time.time()

        if timeout_left < 0:
            raise asyncio.TimeoutError

        return await self._get_specific_response(unique_id, timeout_left)

    async def _send(self, message):
        self.logger.info("%s: send %s", self.id, message)
        await self._connection.send(message)
