"""App views"""

from __future__ import absolute_import, unicode_literals

import datetime
import json
import logging
import threading
import time
import traceback
from distutils.util import strtobool

import requests
from azure.cognitiveservices.vision.customvision.training.models import (
    CustomVisionErrorException, ImageFileCreateEntry, Region)
from django.http import JsonResponse
from filters.mixins import FiltersMixin
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action, api_view
from rest_framework.response import Response

from ...azure_iot.utils import inference_module_url
from ...azure_parts.models import Part
from ...cameras.models import Camera
from ...general import error_messages
from ...images.models import Image
from ...notifications.models import Notification
from ..models import Project, Task, Train
from ..utils import update_app_insight_counter, pull_cv_project_helper
from .serializers import ProjectSerializer, TaskSerializer, TrainSerializer

logger = logging.getLogger(__name__)


class TaskViewSet(FiltersMixin, viewsets.ModelViewSet):
    """Task ModelViewSet

    Available filters:
    @project
    """

    queryset = Task.objects.all()
    serializer_class = TaskSerializer
    filter_backends = (filters.OrderingFilter,)
    filter_mappings = {
        "project": "project",
    }


class ProjectViewSet(FiltersMixin, viewsets.ModelViewSet):
    """Project ModelViewSet

    Available filters
    @is_demo
    """

    queryset = Project.objects.all()
    serializer_class = ProjectSerializer
    filter_backends = (filters.OrderingFilter,)
    filter_mappings = {
        "is_demo": "is_demo",
    }

    @action(detail=True, methods=["get"])
    def delete_tag(self, request, pk=None):
        """delete tag"""
        try:
            project_obj = self.get_object()
            part_id = request.query_params.get("part_id") or None
            part_name = request.query_params.get("part_name") or None
            if part_id is not None:
                project_obj.delete_tag_by_id(tag_id=part_id)
                return Response({'status': 'ok'})
            if part_name is not None:
                project_obj.delete_tag_by_name(tag_name=part_name)
                return Response({'status': 'ok'})
            raise AttributeError('part_name or part_id not found')
        except AttributeError as attr_err:
            return Response(
                {
                    'status': 'failed',
                    'log': str(attr_err)
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        except CustomVisionErrorException as customvision_err:
            return Response(
                {
                    'status': 'failed',
                    'log': str(customvision_err)
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )


class TrainViewSet(viewsets.ModelViewSet):
    """Train ModelViewSet"""

    queryset = Train.objects.all()
    serializer_class = TrainSerializer


@api_view()
def export(request, project_id):
    """get the status of train job sent to custom vision

       @FIXME (Hugh): change the naming of this endpoint
       @FIXME (Hugh): refactor how we store Train.performance
    """
    logger.info("exporting project. Project Id: %s", {project_id})
    project_obj = Project.objects.get(pk=project_id)
    train_obj = Train.objects.get(project_id=project_id)

    success_rate = 0.0
    inference_num = 0
    unidentified_num = 0
    try:
        res = requests.get("http://" + inference_module_url() + "/metrics")
        data = res.json()
        success_rate = int(data["success_rate"] * 100) / 100
        inference_num = data["inference_num"]
        unidentified_num = data["unidentified_num"]
        is_gpu = data["is_gpu"]
        average_inference_time = data["average_inference_time"]
        logger.info("success_rate: %s. inference_num: %s", success_rate,
                    inference_num)
    #         return JsonResponse({
    #             'status': train_obj.status,
    #             'log': train_obj.log,
    #             'download_uri': project_obj.download_uri,
    #             'success_rate': success_rate,
    #             'inference_num': inference_num,
    #             'unidentified_num': unidentified_num,
    #         })
    except requests.exceptions.ConnectionError:
        logger.error(
            "Export failed. Inference module url: %s unreachable",
            inference_module_url(),
        )
        return JsonResponse(
            {
                "status":
                    "failed",
                "log":
                    "Export failed. Inference module url: " +
                    f"{inference_module_url()} unreachable",
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    except:
        logger.exception("unexpected error while exporting project")
        return JsonResponse(
            {
                "status": "failed",
                "log": "unexpected error",
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    return JsonResponse({
        "status": train_obj.status,
        "log": train_obj.log,
        "download_uri": project_obj.download_uri,
        "success_rate": success_rate,
        "inference_num": inference_num,
        "unidentified_num": unidentified_num,
        "gpu": is_gpu,
        "average_time": average_inference_time,
    })


@api_view()
def export_null(request):
    """FIXME tmp workaround"""

    project_obj = Project.objects.all()[0]
    trainer = project_obj.setting.revalidate_and_get_trainer_obj()

    customvision_project_id = project_obj.customvision_project_id

    if trainer is None:
        logger.error("trainer obj is invalid")
        return JsonResponse({"status": "trainer invalid"})
    iterations = trainer.get_iterations(customvision_project_id)
    if len(iterations) == 0:
        logger.info("not yet training ...")
        return JsonResponse({"status": "waiting training"})

    iteration = iterations[0]

    if not iteration.exportable or iteration.status != "Completed":
        logger.info("waiting training ...")
        return JsonResponse({"status": "waiting training"})

    exports = trainer.get_exports(customvision_project_id, iteration.id)
    if len(exports) == 0:
        logger.info("exporting ...")
        res = project_obj.export_iterationv3_2(iteration.id)
        logger.info(res.json())
        return JsonResponse({"status": "exporting"})

    project_obj.download_uri = exports[0].download_uri
    project_obj.save(update_fields=["download_uri"])

    return JsonResponse({
        "status": "ok",
        "download_uri": exports[-1].download_uri
    })


@api_view()
def train_performance(request, project_id):
    """Get train performace of this iter and previous iter"""
    project_obj = Project.objects.get(pk=project_id)
    trainer = project_obj.setting.revalidate_and_get_trainer_obj()
    customvision_project_id = project_obj.customvision_project_id

    if project_obj.is_demo:
        return JsonResponse({
            "status": "ok",
            "precision": 1,
            "recall": "demo_recall",
            "map": "demo_map",
        })

    ret = {}
    iterations = trainer.get_iterations(customvision_project_id)

    def _parse(iteration):
        iteration = iteration.as_dict()
        iteration_status = iteration["status"]
        if iteration_status == "Completed":
            performance = trainer.get_iteration_performance(
                customvision_project_id, iteration["id"]).as_dict()
            precision = performance["precision"]
            recall = performance["recall"]
            mAP = performance["average_precision"]
        else:
            precision = 0.0
            recall = 0.0
            mAP = 0.0
        return {
            "status": iteration_status,
            "precision": precision,
            "recall": recall,
            "map": mAP,
        }

    if len(iterations) >= 1:
        ret["new"] = _parse(iterations[0])
    if len(iterations) >= 2:
        ret["previous"] = _parse(iterations[1])

    return JsonResponse(ret)


@api_view()
def train(request, project_id):
    """
    Train a project
    """
    is_demo = request.query_params.get("demo")
    project_obj = Project.objects.get(pk=project_id)
    parts = [p.name for p in project_obj.parts.all()]
    rtsp = project_obj.camera.rtsp
    download_uri = project_obj.download_uri

    if is_demo and (is_demo.lower() == "true") or project_obj.is_demo:
        logger.info("demo... bypass training process")

        cam_is_demo = project_obj.camera.is_demo
        # Camera FIXME peter, check here
        if cam_is_demo:
            rtsp = project_obj.camera.rtsp
            requests.get(
                "http://" + inference_module_url() + "/update_cam",
                params={
                    "cam_type": "rtsp",
                    "cam_source": rtsp
                },
            )
        else:
            rtsp = project_obj.camera.rtsp
            requests.get(
                "http://" + inference_module_url() + "/update_cam",
                params={
                    "cam_type": "rtsp",
                    "cam_source": rtsp
                },
            )

        requests.get(
            "http://" + inference_module_url() + "/update_model",
            params={"model_dir": "default_model"},
        )
        # '/update_model', params={'model_dir': 'default_model_6parts'})

        logger.info("Update parts %s", parts)
        requests.get(
            "http://" + inference_module_url() + "/update_parts",
            params={"parts": parts},
        )
        requests.get(
            "http://" + inference_module_url() + "/update_retrain_parameters",
            params={
                "confidence_min": 30,
                "confidence_max": 30,
                "max_images": 10
            },
        )

        project_obj.upcreate_training_status(status="ok", log="demo ok")
        project_obj.has_configured = True
        project_obj.save()
        # FIXME pass the new model info to inference server (willy implement)
        return JsonResponse({"status": "ok"})

    project_obj.upcreate_training_status(
        status="preparing", log="preparing data (images and annotations)")
    logger.info("sleeping")

    def _send(rtsp, parts, download_uri):
        logger.info("**** updating cam to %s", rtsp)
        requests.get(
            "http://" + inference_module_url() + "/update_cam",
            params={
                "cam_type": "rtsp",
                "cam_source": rtsp
            },
        )
        requests.get(
            "http://" + inference_module_url() + "/update_model",
            params={"model_uri": download_uri},
        )
        requests.get(
            "http://" + inference_module_url() + "/update_parts",
            params={"parts": parts},
        )

    threading.Thread(target=_send, args=(rtsp, parts, download_uri)).start()
    return upload_and_train(project_id)


def upload_and_train(project_id):
    """Actually do uplaod, train and deploy
    """

    project_obj = Project.objects.get(pk=project_id)
    trainer = project_obj.setting.revalidate_and_get_trainer_obj()
    customvision_project_id = project_obj.customvision_project_id

    # Invalid Endpoint + Training Key
    if not trainer:
        project_obj.upcreate_training_status(
            status="failed", log=error_messages.CUSTOM_VISION_ACCESS_ERROR)
        return JsonResponse(
            {
                "status": "failed",
                "log": error_messages.CUSTOM_VISION_ACCESS_ERROR
            },
            status=503,
        )

    project_obj.upcreate_training_status(
        status="preparing", log="preparing data (images and annotations)")

    project_obj.dequeue_iterations()

    try:
        count = 10
        while count > 0:
            part_ids = [part.id for part in project_obj.parts.all()]
            if len(part_ids) > 0:
                break
            logging.info("waiting parts...")
            time.sleep(1)
            count -= 1

        logger.info("Project id: %s", project_obj.id)
        logger.info("Part ids: %s", part_ids)
        try:
            trainer.get_project(customvision_project_id)
            project_obj.upcreate_training_status(
                status="preparing",
                log=(f"Project {project_obj.customvision_project_name}" +
                     "found on Custom Vision"),
            )
        except:
            project_obj.create_project()
            project_obj.upcreate_training_status(
                status="preparing",
                log=("Project created on CustomVision. " +
                     f"Name: {project_obj.customvision_project_name}"),
            )

            logger.info("Project created on CustomVision.")
            logger.info("Project Id: %s", project_obj.customvision_project_id)
            logger.info("Project Name: %s",
                        project_obj.customvision_project_name)
            customvision_project_id = project_obj.customvision_project_id

        project_obj.upcreate_training_status(
            status="sending", log="sending data (images and annotations)")
        tags = trainer.get_tags(customvision_project_id)
        tag_dict = {}
        project_partnames = {}
        project_changed = False
        has_new_parts = False
        has_new_images = False
        for tag in tags:
            tag_dict[tag.name] = tag.id
        parts_last_train = len(tags)
        images_last_train = trainer.get_tagged_image_count(
            project_obj.customvision_project_id)
        # Update existing tags
        # TODO: update tags
        # trainer.update_tags(project_id, tag_id, new_tag)

        # Create tags on CustomVisioin Project
        # Maybe move to Project Model?
        logger.info("Creating tags before training...")
        counter = 0
        for part_id in part_ids:
            part_name = Part.objects.get(id=part_id).name
            part_description = Part.objects.get(id=part_id).description
            project_partnames[part_name] = "foo"
            if part_name not in tag_dict:
                logger.info("Creating tag: %s. Description: %s", part_name,
                            part_description)
                tag = trainer.create_tag(
                    project_id=customvision_project_id,
                    name=part_name,
                    description=part_description,
                )
                has_new_parts = True
                tag_dict[tag.name] = tag.id
                counter += 1
        project_changed = project_changed or (counter > 0)
        logger.info("Created %s tags", counter)
        logger.info("Creating tags... Done")

        # Upload images to CustomVisioin Project
        images = Image.objects.filter(part_id__in=part_ids,
                                      is_relabel=False,
                                      uploaded=False).all()
        logger.info("Uploading images before training...")
        count = 0
        img_entries = []
        img_objs = []
        logger.info("Image length: %s", len(images))

        for index, image_obj in enumerate(images):
            logger.info("*** image %s, %s", index + 1, image_obj)
            has_new_images = True
            part = image_obj.part
            part_name = part.name
            tag_id = tag_dict[part_name]
            img_name = "img-" + datetime.datetime.utcnow().isoformat()

            regions = []
            width = image_obj.image.width
            height = image_obj.image.height
            try:
                labels = json.loads(image_obj.labels)
                if len(labels) == 0:
                    continue
                for label in labels:
                    x = label["x1"] / width
                    y = label["y1"] / height
                    w = (label["x2"] - label["x1"]) / width
                    h = (label["y2"] - label["y1"]) / height
                    region = Region(tag_id=tag_id,
                                    left=x,
                                    top=y,
                                    width=w,
                                    height=h)
                    regions.append(region)

                image = image_obj.image
                image.open()
                img_entry = ImageFileCreateEntry(name=img_name,
                                                 contents=image.read(),
                                                 regions=regions)
                img_objs.append(image_obj)
                img_entries.append(img_entry)
                project_changed = project_changed or (not image_obj.uploaded)
                if project_changed:
                    logger.info("project_changed: %s", project_changed)
                count += 1
            except:
                logger.exception("unexpected error")

            if len(img_entries) >= 5:
                logger.info("Uploading %s images", len(img_entries))
                upload_result = trainer.create_images_from_files(
                    customvision_project_id, images=img_entries)
                logger.info(
                    "Uploading images... Is batch success: %s",
                    upload_result.is_batch_successful,
                )
                img_entries = []
                for img_obj in img_objs:
                    img_obj.uploaded = True
                    img_obj.save()
                img_objs = []

        if len(img_entries) >= 1:
            logger.info("Uploading %s images", len(img_entries))
            upload_result = trainer.create_images_from_files(
                customvision_project_id, images=img_entries)
            logger.info(
                "Uploading images... Is batch success: %s",
                upload_result.is_batch_successful,
            )
            for img_obj in img_objs:
                img_obj.uploaded = True
                img_obj.save()
        logger.info("Uploading images... Done")

        # Submit training task to Custom Vision
        if not project_changed:
            project_obj.upcreate_training_status(
                status="deploying",
                log="No new parts or new images to train. Deploying")
        else:
            project_obj.upcreate_training_status(
                status="training",
                log="Project changed. Submitting training task...")
            training_task_submit_success = project_obj.train_project()
            if training_task_submit_success:
                update_app_insight_counter(
                    project_obj=project_obj,
                    has_new_parts=has_new_parts,
                    has_new_images=has_new_images,
                    parts_last_train=parts_last_train,
                    images_last_train=images_last_train,
                )
        # A Thread/Task to keep updating the status
        update_train_status(project_id)
        return JsonResponse({"status": "ok"})

    except CustomVisionErrorException as customvision_err:
        logger.error("CustomVisionErrorException: %s", customvision_err)
        if customvision_err.message == \
                "Operation returned an invalid status code 'Access Denied'":
            project_obj.upcreate_training_status(
                status="failed",
                log=
                "Training key or Endpoint is invalid. Please change the settings",
            )
            return JsonResponse(
                {
                    "status":
                        "failed",
                    "log":
                        "Training key or Endpoint is invalid. Please change the settings",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        project_obj.upcreate_training_status(status="failed",
                                             log=customvision_err.message)
        return JsonResponse(
            {
                "status": "failed",
                "log": customvision_err.message
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    except Exception as e:
        # TODO: Remove in production
        err_msg = traceback.format_exc()
        logger.exception("Exception: %s", err_msg)
        project_obj.upcreate_training_status(status="failed",
                                             log=f"failed {str(err_msg)}")
        return JsonResponse({
            "status": "failed",
            "log": f"failed {str(err_msg)}"
        })


def update_train_status(project_id):
    """
    This function not only update status, but also send request to inference
    model
    """

    def _train_status_worker(project_id):
        project_obj = Project.objects.get(pk=project_id)
        trainer = project_obj.setting.revalidate_and_get_trainer_obj()
        camera_id = project_obj.camera_id
        customvision_project_id = project_obj.customvision_project_id
        camera = Camera.objects.get(pk=camera_id)
        wait_prepare = 0
        # If exceed, this project probably not going to be trained
        max_wait_prepare = 60
        while True:
            time.sleep(1)

            iterations = trainer.get_iterations(customvision_project_id)
            if len(iterations) == 0:
                project_obj.upcreate_training_status(
                    status="preparing",
                    log="preparing custom vision environment",
                )
                wait_prepare += 1
                if wait_prepare > max_wait_prepare:
                    project_obj.upcreate_training_status(
                        status="failed",
                        log="Get iteration from Custom Vision occurs error.",
                    )
                    break
                continue

            iteration = iterations[0]
            if not iteration.exportable or iteration.status != "Completed":
                project_obj.upcreate_training_status(
                    status="training",
                    log=
                    "training (Training job might take up to 10-15 minutes)",
                )

                continue

            exports = trainer.get_exports(customvision_project_id,
                                          iteration.id)
            if len(exports) == 0 or not exports[0].download_uri:
                project_obj.upcreate_training_status(status="exporting",
                                                     log="exporting model")
                res = project_obj.export_iterationv3_2(iteration.id)
                logger.info(res.json())
                continue

            project_obj.download_uri = exports[0].download_uri
            project_obj.save(update_fields=["download_uri"])
            parts = [p.name for p in project_obj.parts.all()]

            logger.info("Successfulling export model: %s",
                        project_obj.download_uri)
            logger.info("Preparing to deploy to inference")
            logger.info("Project is deployed before: %s", project_obj.deployed)
            if not project_obj.deployed:
                if exports[0].download_uri:

                    def _send(download_uri, rtsp, parts):
                        requests.get(
                            "http://" + inference_module_url() + "/update_cam",
                            params={
                                "cam_type": "rtsp",
                                "cam_source": rtsp
                            },
                        )
                        requests.get(
                            "http://" + inference_module_url() +
                            "/update_model",
                            params={"model_uri": download_uri},
                        )
                        requests.get(
                            "http://" + inference_module_url() +
                            "/update_parts",
                            params={"parts": parts},
                        )

                    threading.Thread(target=_send,
                                     args=(exports[0].download_uri,
                                           camera.rtsp, parts)).start()

                    project_obj.deployed = True
                    project_obj.save(
                        update_fields=["download_uri", "deployed"])

                project_obj.upcreate_training_status(status="deploying",
                                                     log="deploying model")
                continue

            logger.info("Training Status: Completed")
            train_performance_list = []
            for iteration in iterations[:2]:
                train_performance_list.append(
                    trainer.get_iteration_performance(customvision_project_id,
                                                      iteration.id).as_dict())

            logger.info("Training Performance: %s", train_performance_list)
            project_obj.upcreate_training_status(
                status="ok",
                log="model training completed",
                performance=json.dumps(train_performance_list),
            )
            Notification.objects.create(
                notification_type="project",
                sender="system",
                title="Training Complete",
                details="Project is trained and deployed")
            project_obj.has_configured = True
            project_obj.save()
            break

    threading.Thread(target=_train_status_worker, args=(project_id,)).start()


@api_view()
def pull_cv_project(request, project_id):
    """
    Delete the local project, parts and images. Pull the remote project from
    Custom Vision.
    TODO: open a Thread/Task
    """
    logger.info("Pulling CustomVision Project")

    # Check Customvision Project id
    customvision_project_id = request.query_params.get(
        "customvision_project_id")
    logger.info("customvision_project_id: %s", {customvision_project_id})

    # Check Partial
    try:
        is_partial = bool(strtobool(request.query_params.get("partial")))
    except:
        is_partial = True
    logger.info("Loading Project in Partial Mode: %s", is_partial)

    try:
        pull_cv_project_helper(project_id=project_id,
                               customvision_project_id=customvision_project_id,
                               is_partial=is_partial)
        return Response({"status": "ok"}, status=status.HTTP_200_OK)
    except Exception as unexpected_error:
        err_msg = traceback.format_exc()
        return Response(
            {
                "status": "failed",
                "log": str(err_msg)  # Change line plz...
            },
            status=status.HTTP_400_BAD_REQUEST)


@api_view()
def project_reset_camera(request, project_id):
    """Set Project Camera to demo"""
    try:
        project_obj = Project.objects.get(pk=project_id)
    except:
        if len(Project.objects.filter(is_demo=False)) > 0:
            project_obj = Project.objects.filter(is_demo=False)[0]
    project_obj.camera = Camera.objects.filter(is_demo=True).first()
    project_obj.save(update_fields=["camera"])
    return JsonResponse({"status": "ok"})


@api_view()
def reset_project(request, project_id):
    """Reset the project without deleting it"""
    try:
        project_obj = Project.objects.get(pk=project_id)
    except:
        if len(Project.objects.filter(is_demo=False)) > 0:
            project_obj = Project.objects.filter(is_demo=False)[0]
    try:
        Part.objects.filter(is_demo=False).delete()
        project_name = request.query_params.get("project_name")
        if not project_name:
            raise ValueError("project_name required")
        Image.objects.all().delete()
        project_obj.customvision_project_id = ""
        project_obj.customvision_project_name = project_name
        project_obj.download_uri = ""
        project_obj.needRetraining = Project._meta.get_field(
            "needRetraining").get_default()
        project_obj.accuracyRangeMin = Project._meta.get_field(
            "accuracyRangeMin").get_default()
        project_obj.accuracyRangeMax = Project._meta.get_field(
            "accuracyRangeMax").get_default()
        project_obj.maxImages = Project._meta.get_field(
            "maxImages").get_default()
        project_obj.deployed = False
        project_obj.training_counter = 0
        project_obj.retraining_counter = 0
        project_obj.save()
        project_obj.create_project()
        return JsonResponse({"status": "ok"})
    except KeyError as key_err:
        if str(key_err) in ["Endpoint", "'Endpoint'"]:
            # Probably reseting without training key and endpoint. When user
            # click configure, project will check customvision_id. If empty
            # than create project, Thus we can pass for now. Wait for
            # configure/training to create project...
            return JsonResponse({"status": "ok"})
        logger.exception("Reset project unexpected key error")
        return JsonResponse(
            {
                "status": "failed",
                "log": str(key_err)
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    except ValueError as value_err:
        logger.exception("Reset Project Value Error")
        return JsonResponse(
            {
                "status": "failed",
                "log": str(value_err)
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    except CustomVisionErrorException as customvision_err:
        logger.exception("Error from Custom Vision")
        if (customvision_err.message ==
                "Operation returned an invalid status code 'Access Denied'"):
            return JsonResponse(
                {
                    "status": "failed",
                    "log": error_messages.CUSTOM_VISION_ACCESS_ERROR
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return JsonResponse(
            {
                "status": "failed",
                "log": customvision_err.message
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    except Exception:
        logger.exception("Uncaught Error")
        raise


@api_view()
def update_prob_threshold(request, project_id):
    """update inference bounding box threshold"""
    prob_threshold = request.query_params.get("prob_threshold")
    project_obj = Project.objects.filter(pk=project_id).first()

    if prob_threshold is None:
        return Response(
            {
                'status': 'failed',
                'log': 'prob_threshold must be given as Integer'
            },
            status=status.HTTP_400_BAD_REQUEST)

    if project_obj is None:
        return Response(
            {
                'status': 'failed',
                'log': 'project with project_id not found'
            },
            status=status.HTTP_400_BAD_REQUEST)
    try:
        prob_threshold = int(prob_threshold)
        if prob_threshold > 100 or prob_threshold < 0:
            return Response(
                {
                    'status': 'failed',
                    'log': 'prob_threshold out of range'
                },
                status=status.HTTP_400_BAD_REQUEST)

        project_obj = Project.objects.filter(pk=project_id).first()

        # Real function call
        project_obj.update_prob_threshold(prob_threshold=prob_threshold)
        return Response({'status': 'ok'})
    except ValueError:
        return Response(
            {
                'status': 'failed',
                'log': 'prob_threshold must be given as Integer'
            },
            status=status.HTTP_400_BAD_REQUEST)
