"""Define a gateway class for managing MQTT connections within the gateway"""

import asyncio
import json
import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, Event
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .mdns import MdnsScanner
from .const import MQTT_CLIENT_INSTANCE, CONF_LIGHT_DEVICE_TYPE, EVENT_ENTITY_REGISTER, MQTT_TOPIC_PREFIX, \
    EVENT_ENTITY_STATE_UPDATE, DEVICE_COUNT_MAX
from .mqtt import MqttClient

from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)


class Gateway:
    """Class for gateway and managing MQTT connections within the gateway"""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Init dummy hub."""
        self.hass = hass
        self._entry = entry
        self._last_init_time = None
        
        self._id = entry.data[CONF_NAME]

        self.light_group_map = {}
        self.room_map = {}
        # self.room_list = []
        self.devTypes = [1, 2, 3, 4, 7, 9, 11]

        self.reconnect_flag = True

        self.init_state = False

        self.device_map = {}

        self.sns = []

        """Lighting Control Type"""
        self.light_device_type = entry.data[CONF_LIGHT_DEVICE_TYPE]

        self.hass.data[MQTT_CLIENT_INSTANCE] = MqttClient(
            self.hass,
            self._entry,
            self._entry.data,
        )

        async def async_stop_mqtt(_event: Event):
            """Stop MQTT component."""
            await self.disconnect()

        self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, async_stop_mqtt)

    async def reconnect(self, entry: ConfigEntry):
        """Reconnect gateway MQTT"""
        _LOGGER.warning("重新连接 async  reconnect")
        mqtt_client: MqttClient = self.hass.data[MQTT_CLIENT_INSTANCE]
        mqtt_client.conf = entry.data
        await mqtt_client.async_disconnect()
        mqtt_client.init_client()
        await mqtt_client.async_connect()

    async def disconnect(self):
        """Disconnect gateway MQTT connection"""

        mqtt_client: MqttClient = self.hass.data[MQTT_CLIENT_INSTANCE]

        await mqtt_client.async_disconnect()

    async def report_q5_init(self, device_list):
        for device in device_list:
            device_type = device["devType"]
            device["unique_id"] = f"{device['sn']}"

            state = int(device["state"])
            if state == 0:
                continue

            if device_type == 3:
                """Curtain"""
                await self._add_entity("cover", device)
            elif device_type == 1 and self.light_device_type == "single":
                """Light"""
                device["is_group"] = False
                await self._add_entity("light", device)
            elif device_type == 11:
                """Climate"""
                await self._add_entity("climate", device)
            elif device_type == 4:
                """reboot gateway"""
                await self._add_entity("button", device)
            elif device_type == 7:
                """sensor"""
                if "a14" in device:
                    await self._add_entity("sensor", device)
                if "a15" in device:
                    await self._add_entity("binary_sensor", device)
            elif device_type == 9:
                """Constant Temperature Control Panel"""
                a110 = int(device["a110"])
                a111 = int(device["a111"])
                a112 = int(device["a112"])
                if a110 == 2 or a111 == 1:
                    await self._add_entity("climate", device)
                if a112 == 1:
                    await self._add_entity("fan", device)
            elif device_type == 2:
                """Switch"""
                if "a15" in device:
                    await self._add_entity("binary_sensor", device)
                if "relays" in device and "relaysNames" in device and "relaysNum" in device:
                    await self._add_entity("switch", device)
                else:
                    self.sns.append(device['sn'])
            elif device_type == 5:
                """MediaPlayer"""
                await self._add_entity("media_player", device)
            if "subgroup" in device:
                self.device_map[device['sn']] = {
                    "room": device['room'],
                    "subgroup": device['subgroup']
                }

    async def _async_mqtt_subscribe(self, msg):
        """Process received MQTT messages"""

        payload = msg.payload
        topic = msg.topic

        if payload:
            try:
                payload = json.loads(payload)

                # store = Store(self.hass, 1, f'test/{topic}')
                # await store.async_save(payload["data"])

            except ValueError:
                _LOGGER.warning("Unable to parse JSON: '%s'", payload)
                return
        else:
            _LOGGER.warning("JSON None")
            return

        if topic.endswith("p5"):
            seq = payload["seq"]
            start = payload["data"]["start"]
            count = payload["data"]["count"]
            total = payload["data"]["total"]

            _LOGGER.debug(f"q5 data:{payload}")

            """Device List data"""
            device_list = payload["data"]["list"]

            if seq == 1:
                await self.report_q5_init(device_list)
                if start + count < total:
                    data = {
                        "start": start + count,
                        "max": DEVICE_COUNT_MAX,
                        "devTypes": self.devTypes,
                    }
                    await self._async_mqtt_publish("P/0/center/q5", data, seq)
            elif seq == 2:

                await self.report_q5_init(device_list)
                if start + count < total:
                    data = {
                        "start": start + count,
                        "max": DEVICE_COUNT_MAX,
                        "sns": self.sns,
                    }
                    await self._async_mqtt_publish("P/0/center/q5", data, seq)
            elif seq == 3:
                for device in device_list:
                    await self._exec_event_3(device)

        elif topic.endswith("p28"):
            """Scene List data"""
            scene_list = payload["data"]
            room_map = self.room_map
            for scene in scene_list:
                scene["unique_id"] = f"{scene['id']}"
                room_id = scene["room"]
                if room_id == 0:
                    scene["room_name"] = "全屋"
                else:
                    scene["room_name"] = room_map.get(
                        room_id, {}).get('name', "未知房间")
                await self._add_entity("scene", scene)
        elif topic.endswith("event/3"):
            """Device state data"""
            stats_list = payload["data"]

            string_array = ["sn", "workingTime", "powerSavings"]
            # 过滤不用查询的字段
            string_filter = ["a109", "a15", "on",
                             "rgb", "level", "kelvin", "travel"]

            string_light_filter = ["on", "rgb", "level", "kelvin"]

            flag = False

            sns = []

            for state in stats_list:

                if any(key in state for key in string_filter):
                    await self._exec_event_3(state)
                else:
                    sns.append(state["sn"])

                if any(key in state for key in string_light_filter):
                    flag = True

                if "workingTime" in state or "powerSavings" in state:
                    for key in state.keys():
                        if key not in string_array:
                            flag = True

            if sns:
                data = {
                    "start": 0,
                    "max": DEVICE_COUNT_MAX,
                    "sns": sns,
                }
                await self._async_mqtt_publish("P/0/center/q5", data, 3)

            _LOGGER.debug(f"event/3 data:{payload}")

            if flag:
                await self.sync_group_status(False)
        elif topic.endswith("event/4"):
            _LOGGER.debug(f"event/4 data:{payload}")

        elif topic.endswith("event/5"):
            group_list = payload["data"]
            _LOGGER.debug(f"event/5 data:{payload}")
            for group in group_list:
                if 'a7' in group and 'a8' in group and 'a9' in group:
                    device_type = group['a7']
                    room_id = group['a8']
                    group_id = group['a9']
                    data = {}
                    if 'a10' in group:
                        data['on'] = group['a10']
                    if 'a11' in group:
                        data['level'] = group['a11']
                    if 'a12' in group:
                        data['kelvin'] = group['a12']
                    if 'a13' in group and group['a13'] != 0:
                        data['rgb'] = group['a13']
                    if device_type == 1 and data:
                        await self._init_or_update_light_group(2, room_id, '', group_id, '', data)

        elif topic.endswith("p33"):
            """Basic data, including room information, light group information, curtain group information"""
            for room in payload["data"]["rooms"]:
                self.room_map[room["id"]] = room
            for lightGroup in payload["data"]["lightsSubgroups"]:
                self.light_group_map[lightGroup["id"]] = lightGroup
            self.room_map[0] = {'id': 0, 'name': '全屋', 'icon': 1}
            self.light_group_map[0] = {'id': 0, 'name': '所有灯', 'icon': 1}

        # elif topic.endswith("p31"):
        #   """Relationship data for rooms and groups"""
        #   self.room_list = []
        #    for room in payload["data"]:
        #        room_id = room["room"]
        #        self.room_list.append(room_id)
        #    await self.sync_group_status(True)

        elif topic.endswith("p82"):
            tmp_lights = {}
            room_map = self.room_map
            light_group_map = self.light_group_map
            seq = payload["seq"]
            for roomObj in payload["data"]:
                if "a7" in roomObj:
                    room_id = roomObj["a8"]

                    tmp_lights["on"] = roomObj["a10"]

                    tmp_lights["level"] = roomObj["a11"]
                    if "a12" in roomObj:
                        tmp_lights["kelvin"] = roomObj["a12"]

                    if "a13" in roomObj:
                        tmp_lights["rgb"] = roomObj["a13"]

                    lights = tmp_lights
                    room_name = room_map.get(room_id, {}).get('name', "未知房间")
                    light_group_id = roomObj["a9"]
                    light_group_name = light_group_map.get(
                        light_group_id, {}).get('name', "未知灯组")
                    await self._init_or_update_light_group(seq, room_id, room_name, light_group_id,
                                                           light_group_name, lights)

        """
        elif topic.endswith("p51"):
            seq = payload["seq"]

            #gu
            _LOGGER.warning("p51 seq: %s", payload)


            for roomObj in payload["data"]:
                if "lights" in roomObj:
                    room_id = roomObj["id"]
                    lights = roomObj["lights"]
                    room_name = roomObj["name"]
                    light_group_id = 0
                    light_group_name = "所有灯"
                    await self._init_or_update_light_group(seq, room_id, room_name, light_group_id,
                                                           light_group_name, lights)
                    if "subgroups" in lights:
                        for subgroupObj in lights["subgroups"]:
                            light_group_id = int(subgroupObj["id"])
                            light_group_name = subgroupObj["name"]
                            await self._init_or_update_light_group(seq, room_id, room_name, light_group_id,
                                                                   light_group_name, subgroupObj)
        """

    async def _exec_event_3(self, data):
        if "relays" in data:
            for relay, is_on in enumerate(data["relays"]):
                status = {
                    "on": is_on
                }
                async_dispatcher_send(
                    self.hass, EVENT_ENTITY_STATE_UPDATE.format(
                        f"switch{data['sn']}{relay}"), status
                )
        # 恒温多实体触发
        elif "devType" in data :
            if data["devType"] == 9 :
                async_dispatcher_send(
                    self.hass, EVENT_ENTITY_STATE_UPDATE.format(
                        data["sn"]), data
                )
                async_dispatcher_send(
                    self.hass, EVENT_ENTITY_STATE_UPDATE.format(
                        data["sn"]+"H"), data
                )
                async_dispatcher_send(
                    self.hass, EVENT_ENTITY_STATE_UPDATE.format(
                        data["sn"]+"F"), data
                )
            elif data["devType"] == 7 :
                async_dispatcher_send(
                    self.hass, EVENT_ENTITY_STATE_UPDATE.format(
                        data["sn"]+"L"), data
                )
                async_dispatcher_send(
                    self.hass, EVENT_ENTITY_STATE_UPDATE.format(
                        data["sn"]+"M"), data
                )
            else:
                async_dispatcher_send(
                    self.hass, EVENT_ENTITY_STATE_UPDATE.format(
                        data["sn"]), data
                )
        elif "a109" in data:
                async_dispatcher_send(
                    self.hass, EVENT_ENTITY_STATE_UPDATE.format(
                        data["sn"]), data
                )
                async_dispatcher_send(
                    self.hass, EVENT_ENTITY_STATE_UPDATE.format(
                        data["sn"]+"H"), data
                )
                async_dispatcher_send(
                    self.hass, EVENT_ENTITY_STATE_UPDATE.format(
                        data["sn"]+"F"), data
                )

        elif "a15" in data:
            async_dispatcher_send(
                self.hass, EVENT_ENTITY_STATE_UPDATE.format(
                    data["sn"]+"L"), data
            )
            async_dispatcher_send(
                    self.hass, EVENT_ENTITY_STATE_UPDATE.format(
                        data["sn"]+"M"), data
            )

        else:
            async_dispatcher_send(
                self.hass, EVENT_ENTITY_STATE_UPDATE.format(data["sn"]), data
            )

    async def _init_or_update_light_group(self, seq: int, room_id: int, room_name: str, light_group_id: int,
                                          light_group_name: str, light_group: dict):
        if seq == 1:
            group = {
                "unique_id": f"{room_id}-{light_group_id}",
                "room": room_id,
                "subgroup": light_group_id,
                "is_group": True,
                "name": f"{room_name}-{light_group_name}",
            }
            group = dict(light_group, **group)
            await self._add_entity("light", group)
        else:
            await self._event_trigger(room_id, light_group_id, light_group)

    async def _event_trigger(self, room: int, subgroup: int, device: dict):
        state = {}
        if "on" in device:
            state["on"] = int(device["on"])
        if "level" in device:
            state["level"] = float(device["level"])
        if "kelvin" in device:
            state["kelvin"] = int(device["kelvin"])
        if "rgb" in device:
            state["rgb"] = int(device["rgb"])
        async_dispatcher_send(
            self.hass, EVENT_ENTITY_STATE_UPDATE.format(
                f"{room}-{subgroup}"), state
        )

    async def sync_group_status(self, is_init: bool):

        data = [
            {
                "a7": 1
            }
        ]

        """
        for room in self.room_list:
            data.append({
                "id": int(room),
                "lights": {
                    "subgroups": []
                }
            })
        """
        if is_init:
            await self._async_mqtt_publish("P/0/center/q82", data, 1)
            # await self._async_mqtt_publish("P/0/center/q51", data, 1)
        else:
            await self._async_mqtt_publish("P/0/center/q82", data, 2)
           # await self._async_mqtt_publish("P/0/center/q51", data, 2)

    async def _add_entity(self, component: str, device: dict):
        """Add child device information"""
        async_dispatcher_send(
            self.hass, EVENT_ENTITY_REGISTER.format(component), device
        )

    async def init(self, entry: ConfigEntry, is_init: bool):
        """Initialize the gateway business logic, including subscribing to device data, scene data, and basic data,
        and sending data reporting instructions to the gateway"""
        self._entry = entry
        discovery_topics = [
            # Subscribe to device list
            f"{MQTT_TOPIC_PREFIX}/center/p5",
            # Subscribe to scene list
            f"{MQTT_TOPIC_PREFIX}/center/p28",
            # Subscribe to all basic data Room list, light group list, curtain group list
            f"{MQTT_TOPIC_PREFIX}/center/p33",
            # Subscribe to room and light group relationship
            # f"{MQTT_TOPIC_PREFIX}/center/p31",
            # Subscribe to room and light group relationship
            # f"{MQTT_TOPIC_PREFIX}/center/p51",
            # Subscribe to room and light group relationship
            f"{MQTT_TOPIC_PREFIX}/center/p82",
            # Subscribe to device property change events
            "p/+/event/3",
            "p/+/event/4",
            "p/+/event/5",
        ]

        try_connect_times = 3

        if self.reconnect_flag:
            await self.reconnect(entry)
            self.reconnect_flag = False
            _LOGGER.warning("重新连接mqtt+++++++++++++++++++++++++++++++++++++++")
        else:
            _LOGGER.warning("没有重新连接mqtt--------------------------------------")

        mqtt_connected = self.hass.data[MQTT_CLIENT_INSTANCE].connected
        while not mqtt_connected:
            await asyncio.sleep(1)
            mqtt_connected = self.hass.data[MQTT_CLIENT_INSTANCE].connected
            _LOGGER.warning("is_init 1 %s mqtt_connected %s",
                            is_init, mqtt_connected)
            try_connect_times = try_connect_times - 1
            if try_connect_times <= 0:
                break

        _LOGGER.warning("is_init 2 %s mqtt_connected %s",
                        is_init, mqtt_connected)
        if mqtt_connected:
            flag = True
            now_time = int(time.time())
            if is_init:
                self._last_init_time = now_time
            else:
                if self._last_init_time is not None:
                    left_time = now_time - self._last_init_time
                    if left_time < 20:
                        return
                else:
                    self._last_init_time = now_time
        else:
            # _LOGGER.warning("repeat scan mdns")
            _LOGGER.warning("未连接，直接退出，等待监控程序检测连接")
            flag = False
            # entry_data = entry.data
            # scanner = MdnsScanner()
            # connection = scanner.scan_single(entry_data[CONF_NAME], 5)
            # if connection is not None:
            #     if CONF_LIGHT_DEVICE_TYPE in entry_data:
            #         connection[CONF_LIGHT_DEVICE_TYPE] = entry_data[CONF_LIGHT_DEVICE_TYPE]
            #         connection["random"] = time.time()
            #     self.hass.config_entries.async_update_entry(
            #         entry,
            #         data=connection,
            #     )

        if flag:
            _LOGGER.warning("start init data")
            self.init_state = True
            try:
                #if is_init:

                await asyncio.gather(
                        *(
                            self.hass.data[MQTT_CLIENT_INSTANCE].async_subscribe(
                                topic,
                                self._async_mqtt_subscribe,
                                0,
                                "utf-8"
                            )
                            for topic in discovery_topics
                        )
                    )
                # publish payload to get all basic data Room list, light group list, curtain group list
                await self._async_mqtt_publish("P/0/center/q33", {})
                await asyncio.sleep(3)
                # publish payload to get device list
                data = {
                    "start": 0,
                    "max": DEVICE_COUNT_MAX,
                    "devTypes": self.devTypes,
                }
                await self._async_mqtt_publish("P/0/center/q5", data, 1)
                await asyncio.sleep(3)
                # publish payload to get scene list
                await self._async_mqtt_publish("P/0/center/q28", {})
                
                # await asyncio.sleep(1)

                if self.light_device_type == "group":
                    # publish payload to get room and light group relationship
                    await asyncio.sleep(5)
                    await self.sync_group_status(True)

                    # await self._async_mqtt_publish("P/0/center/q31", {})
                await asyncio.sleep(10)
                if self.sns:
                    data = {
                        "start": 0,
                        "max": DEVICE_COUNT_MAX,
                        "sns": self.sns,
                    }
                    await self._async_mqtt_publish("P/0/center/q5", data, 2)
            except OSError as err:
                self.init_state = False
                _LOGGER.error("出了一些问题: %s", err)

    async def async_mqtt_publish(self, topic: str, data: object):
        return await self._async_mqtt_publish(self.hass, topic, data, seq=2)

    async def _async_mqtt_publish(self, topic: str, data: object, seq=2):

        query_device_payload = {
            "seq": seq,
            "rspTo": MQTT_TOPIC_PREFIX,
            "data": data
        }
        _LOGGER.debug("topic %s data %s", topic, query_device_payload)
        await self.hass.data[MQTT_CLIENT_INSTANCE].async_publish(
            topic,
            json.dumps(query_device_payload),
            0,
            False
        )
