import logging
import time
from datetime import datetime
from io import BytesIO
from logging import Logger
from typing import Any, Optional, Sequence, Tuple

import numpy as np
import PIL.Image as PILImage
from robot_interface.models.geometry.frame import Frame
from robot_interface.models.geometry.joints import Joints
from robot_interface.models.geometry.orientation import Orientation
from robot_interface.models.geometry.pose import Pose
from robot_interface.models.geometry.position import Position
from robot_interface.models.inspection.formats.image import Image, ThermalImage
from robot_interface.models.inspection.inspection import (
    Inspection,
    InspectionResult,
    TimeIndexedPose,
)
from robot_interface.models.inspection.metadata import ImageMetadata
from robot_interface.models.inspection.references.image_reference import (
    ImageReference,
    ThermalImageReference,
)
from robot_interface.models.mission import (
    DriveToPose,
    MissionStatus,
    TakeImage,
    TakeThermalImage,
    Task,
)
from robot_interface.robot_interface import RobotInterface

from isar_turtlebot.config import config
from isar_turtlebot.models.turtlebot_status import TurtlebotStatus
from isar_turtlebot.ros_bridge.ros_bridge import RosBridge
from isar_turtlebot.utilities.inspection_pose import get_inspection_pose


class Robot(RobotInterface):
    def __init__(self):
        self.logger: Logger = logging.getLogger("robot")

        self.bridge: RosBridge = RosBridge()

        self.inspection_task_timeout: float = config.getfloat(
            "mission", "inspection_task_timeout"
        )
        self.current_task: Optional[str] = None
        self.inspection_status: Optional[TurtlebotStatus] = None

    def schedule_task(self, task: Task) -> Tuple[bool, Optional[Any], Optional[Joints]]:
        run_id: str = self._publish_task(task=task)
        return True, run_id, None

    def mission_scheduled(self) -> bool:
        return False

    def mission_status(self, mission_id: Any) -> MissionStatus:
        mission_status: MissionStatus = TurtlebotStatus.get_mission_status(
            status=self._task_status()
        )
        return mission_status

    def abort_mission(self) -> bool:
        return True

    def log_status(
        self, mission_id: Any, mission_status: MissionStatus, current_task: Task
    ):
        self.logger.info(f"Mission ID: {mission_id}")
        self.logger.info(f"Mission Status: {mission_status}")
        self.logger.info(f"Current Task: {current_task}")

    def get_inspection_references(
        self, vendor_mission_id: Any, current_task: Task
    ) -> Sequence[Inspection]:
        now: datetime = datetime.utcnow()

        if isinstance(current_task, TakeImage):
            self.bridge.visual_inspection.register_run_id(run_id=vendor_mission_id)
            pose: Pose = self._get_robot_pose()
            image_metadata: ImageMetadata = ImageMetadata(
                start_time=now,
                time_indexed_pose=TimeIndexedPose(pose=pose, time=now),
                file_type=config.get("metadata", "image_filetype"),
            )
            image_ref: ImageReference = ImageReference(
                id=vendor_mission_id, metadata=image_metadata
            )
        elif isinstance(current_task, TakeThermalImage):
            self.bridge.visual_inspection.register_run_id(run_id=vendor_mission_id)
            pose: Pose = self._get_robot_pose()
            image_metadata: ImageMetadata = ImageMetadata(
                start_time=now,
                time_indexed_pose=TimeIndexedPose(pose=pose, time=now),
                file_type=config.get("metadata", "thermal_image_filetype"),
            )
            image_ref: ThermalImageReference = ThermalImageReference(
                id=vendor_mission_id, metadata=image_metadata
            )

        return [image_ref]

    def download_inspection_result(
        self, inspection: Inspection
    ) -> Optional[InspectionResult]:
        if isinstance(inspection, ImageReference):
            try:
                image_data = self.bridge.visual_inspection.read_image(
                    run_id=inspection.id
                )

                inspection_result = Image(
                    id=inspection.id, metadata=inspection.metadata, data=image_data
                )
            except (KeyError, TypeError, FileNotFoundError) as e:
                self.logger("Failed to retreive inspection result", e)
                inspection_result = None
        elif isinstance(inspection, ThermalImageReference):
            try:
                image_data = self.bridge.visual_inspection.read_image(
                    run_id=inspection.id
                )
                image = PILImage.open(BytesIO(image_data))
                image_array = np.asarray(image)
                image_red = image_array[:, :, 0]
                image_grey = PILImage.fromarray(image_red)
                image_array_io = BytesIO()
                image_grey.save(image_array_io, format=inspection.metadata.file_type)
                inspection_result = ThermalImage(
                    id=inspection.id,
                    metadata=inspection.metadata,
                    data=image_array_io.getvalue(),
                )
            except Exception as e:
                self.logger("Failed to retreive inspection result", e)
                inspection_result = None
        return inspection_result

    def robot_pose(self) -> Pose:
        return self._get_robot_pose()

    def _get_robot_pose(self) -> Pose:

        pose_message: dict = self.bridge.pose.get_value()
        position_message: dict = pose_message["pose"]["pose"]["position"]
        orientation_message: dict = pose_message["pose"]["pose"]["orientation"]

        pose: Pose = Pose(
            position=Position(
                x=position_message["x"],
                y=position_message["y"],
                z=position_message["z"],
                frame=Frame.Robot,
            ),
            orientation=Orientation(
                x=orientation_message["x"],
                y=orientation_message["y"],
                z=orientation_message["z"],
                w=orientation_message["w"],
                frame=Frame.Robot,
            ),
            frame=Frame.Robot,
        )
        return pose

    def _task_status(self) -> TurtlebotStatus:
        if self.current_task == "inspection":
            return self.inspection_status
        return self._navigation_status()

    def _navigation_status(self) -> TurtlebotStatus:
        message: dict = self.bridge.mission_status.get_value()
        turtle_status: TurtlebotStatus = TurtlebotStatus.map_to_turtlebot_status(
            message["status_list"][0]["status"]
        )
        return turtle_status

    def _publish_task(self, task: Task) -> str:
        if isinstance(task, DriveToPose):
            self.current_task = "navigation"
            previous_run_id: str = self._get_run_id()
            self._publish_navigation_task(pose=task.pose)
            run_id: str = self._wait_for_updated_task(previous_run_id=previous_run_id)

            return run_id
        elif isinstance(task, (TakeImage, TakeThermalImage)):
            self.current_task = "inspection"
            run_id: str = self._publish_inspection_task(target=task.target)
            try:
                self._do_inspection_task()
            except TimeoutError as e:
                self.logger.error(e)
                self.current_task = None

            return run_id

        else:
            raise NotImplementedError(
                f"Scheduled task: {task} is not implemented on {self}"
            )

    def _do_inspection_task(self):
        start_time: float = time.time()
        while self._navigation_status() is not TurtlebotStatus.Succeeded:
            time.sleep(0.1)
            execution_time: float = time.time() - start_time
            if execution_time > self.inspection_task_timeout:
                self.inspection_status = TurtlebotStatus.Failure
                raise TimeoutError(
                    f"Drive to inspection pose task for TurtleBot3 timed out. Run ID: {self._get_run_id()}"
                )

        self.bridge.visual_inspection.take_image()
        while not self.bridge.visual_inspection.stored_image():
            time.sleep(0.1)
            execution_time: float = time.time() - start_time
            if execution_time > self.inspection_task_timeout:
                self.inspection_status = TurtlebotStatus.Failure
                raise TimeoutError(
                    f"Storing image for TurtleBot3 timed out. Run ID: {self._get_run_id()}"
                )
        self.inspection_status = TurtlebotStatus.Succeeded
        self.current_task = None

    def _publish_inspection_task(self, target: Position) -> None:
        self.inspection_status = TurtlebotStatus.Active
        previous_run_id: str = self._get_run_id()
        inspection_pose: Pose = get_inspection_pose(
            current_pose=self.robot_pose(), target=target
        )
        self._publish_navigation_task(pose=inspection_pose)
        run_id: str = self._wait_for_updated_task(previous_run_id=previous_run_id)
        return run_id

    def _publish_navigation_task(self, pose: Pose) -> None:
        pose_message: dict = {
            "goal": {
                "target_pose": {
                    "header": {
                        "seq": 0,
                        "stamp": {"secs": 1533, "nsecs": 746000000},
                        "frame_id": "map",
                    },
                    "pose": {
                        "position": {
                            "x": pose.position.x,
                            "y": pose.position.y,
                            "z": pose.position.z,
                        },
                        "orientation": {
                            "x": pose.orientation.x,
                            "y": pose.orientation.y,
                            "z": pose.orientation.z,
                            "w": pose.orientation.w,
                        },
                    },
                }
            },
        }

        self.bridge.execute_task.publish(message=pose_message)

    def _get_run_id(self) -> Optional[str]:
        status_msg: dict = self.bridge.mission_status.get_value()
        try:
            run_id: str = status_msg["status_list"][0]["goal_id"]["id"]
            return run_id.replace("move_base-", "").split(".")[0].replace("-", "")
        except (KeyError, IndexError):
            self.logger.info("Failed to get current mission_id returning None")
            return None

    def _wait_for_updated_task(self, previous_run_id: str, timeout: int = 20) -> str:
        start_time: float = time.time()
        current_run_id: str = self._get_run_id()

        while current_run_id == previous_run_id:
            time.sleep(0.1)
            execution_time: float = time.time() - start_time
            if execution_time > timeout:
                raise TimeoutError(
                    f"Scheduling of task for TurtleBot3 timed out. Run ID: {current_run_id}"
                )
            current_run_id = self._get_run_id()

        return current_run_id
