# -*- coding: utf-8 -*-
"""Submit publishing job to farm."""
import os
import json
import re
from copy import deepcopy

import clique
import ayon_api
import pyblish.api

from ayon_core.pipeline import publish
from ayon_core.lib import EnumDef, is_in_tests
from ayon_core.pipeline.version_start import get_versioning_start

from ayon_core.pipeline.farm.pyblish_functions import (
    create_skeleton_instance,
    create_instances_for_aov,
    attach_instances_to_product,
    prepare_representations,
    create_metadata_path
)
from ayon_deadline.abstract_submit_deadline import requests_post


def get_resource_files(resources, frame_range=None):
    """Get resource files at given path.

    If `frame_range` is specified those outside will be removed.

    Arguments:
        resources (list): List of resources
        frame_range (list): Frame range to apply override

    Returns:
        list of str: list of collected resources

    """
    res_collections, _ = clique.assemble(resources)
    assert len(res_collections) == 1, "Multiple collections found"
    res_collection = res_collections[0]

    # Remove any frames
    if frame_range is not None:
        for frame in frame_range:
            if frame not in res_collection.indexes:
                continue
            res_collection.indexes.remove(frame)

    return list(res_collection)


class ProcessSubmittedJobOnFarm(pyblish.api.InstancePlugin,
                                publish.AYONPyblishPluginMixin,
                                publish.ColormanagedPyblishPluginMixin):
    """Process Job submitted on farm.

    These jobs are dependent on a deadline job
    submission prior to this plug-in.

    It creates dependent job on farm publishing rendered image sequence.

    Options in instance.data:
        - deadlineSubmissionJob (dict, Required): The returned .json
          data from the job submission to deadline.

        - outputDir (str, Required): The output directory where the metadata
            file should be generated. It's assumed that this will also be
            final folder containing the output files.

        - ext (str, Optional): The extension (including `.`) that is required
            in the output filename to be picked up for image sequence
            publishing.

        - publishJobState (str, Optional): "Active" or "Suspended"
            This defaults to "Suspended"

        - expectedFiles (list or dict): explained below

    """

    label = "Submit Image Publishing job to Deadline"
    order = pyblish.api.IntegratorOrder + 0.2
    icon = "tractor"

    targets = ["local"]

    hosts = ["fusion", "max", "maya", "nuke", "houdini",
             "celaction", "aftereffects", "harmony", "blender", "unreal"]

    families = ["render", "render.farm", "render.frames_farm",
                "prerender", "prerender.farm", "prerender.frames_farm",
                "renderlayer", "imagesequence", "image",
                "vrayscene", "maxrender",
                "arnold_rop", "mantra_rop",
                "karma_rop", "vray_rop",
                "redshift_rop", "usdrender"]
    settings_category = "deadline"

    aov_filter = [
        {
            "name": "maya",
            "value": [r".*([Bb]eauty).*"]
        },
        {
            "name": "blender",
            "value": [r".*([Bb]eauty).*"]
        },
        {
            # for everything from AE
            "name": "aftereffects",
            "value": [r".*"]
        },
        {
            "name": "harmony",
            "value": [r".*"]
        },
        {
            "name": "celaction",
            "value": [r".*"]
        },
        {
            "name": "max",
            "value": [r".*"]
        },
    ]

    environ_keys = [
        "FTRACK_API_USER",
        "FTRACK_API_KEY",
        "FTRACK_SERVER",
        "AYON_APP_NAME",
        "AYON_USERNAME",
        "AYON_SG_USERNAME",
        "KITSU_LOGIN",
        "KITSU_PWD"
    ]

    # custom deadline attributes
    deadline_department = ""
    deadline_pool = ""
    deadline_pool_secondary = ""
    deadline_group = ""
    deadline_priority = None

    # regex for finding frame number in string
    R_FRAME_NUMBER = re.compile(r'.+\.(?P<frame>[0-9]+)\..+')

    # mapping of instance properties to be transferred to new instance
    #     for every specified family
    instance_transfer = {
        "slate": ["slateFrames", "slate"],
        "review": ["lutPath"],
        "render2d": ["bakingNukeScripts", "version"],
        "renderlayer": ["convertToScanline"]
    }

    # list of family names to transfer to new family if present
    families_transfer = ["render3d", "render2d", "slate"]
    plugin_pype_version = "3.0"

    # poor man exclusion
    skip_integration_repre_list = []

    def _submit_deadline_post_job(self, instance, job, instances):
        """Submit publish job to Deadline.

        Returns:
            (str): deadline_publish_job_id
        """
        data = instance.data.copy()
        product_name = data["productName"]
        job_name = "Publish - {}".format(product_name)

        anatomy = instance.context.data['anatomy']

        # instance.data.get("productName") != instances[0]["productName"]
        # 'Main' vs 'renderMain'
        override_version = None
        instance_version = instance.data.get("version")  # take this if exists
        if instance_version != 1:
            override_version = instance_version

        output_dir = self._get_publish_folder(
            anatomy,
            deepcopy(instance.data["anatomyData"]),
            instance.data.get("folderEntity"),
            instances[0]["productName"],
            instance.context,
            instances[0]["productType"],
            override_version
        )

        # Transfer the environment from the original job to this dependent
        # job so they use the same environment
        metadata_path, rootless_metadata_path = \
            create_metadata_path(instance, anatomy)

        settings_variant = os.environ["AYON_DEFAULT_SETTINGS_VARIANT"]
        environment = {
            "AYON_PROJECT_NAME": instance.context.data["projectName"],
            "AYON_FOLDER_PATH": instance.context.data["folderPath"],
            "AYON_TASK_NAME": instance.context.data["task"],
            "AYON_USERNAME": instance.context.data["user"],
            "AYON_LOG_NO_COLORS": "1",
            "AYON_IN_TESTS": str(int(is_in_tests())),
            "AYON_PUBLISH_JOB": "1",
            "AYON_RENDER_JOB": "0",
            "AYON_REMOTE_PUBLISH": "0",
            "AYON_BUNDLE_NAME": os.environ["AYON_BUNDLE_NAME"],
            "AYON_DEFAULT_SETTINGS_VARIANT": settings_variant,
        }

        # add environments from self.environ_keys
        for env_key in self.environ_keys:
            if os.getenv(env_key):
                environment[env_key] = os.environ[env_key]

        priority = self.deadline_priority or instance.data.get("priority", 50)

        instance_settings = self.get_attr_values_from_data(instance.data)
        initial_status = instance_settings.get("publishJobState", "Active")

        args = [
            "--headless",
            'publish',
            '"{}"'.format(rootless_metadata_path),
            "--targets", "deadline",
            "--targets", "farm",
        ]
        # TODO remove when AYON launcher respects environment variable
        #   'AYON_DEFAULT_SETTINGS_VARIANT'
        if settings_variant == "staging":
            args.append("--use-staging")

        # Generate the payload for Deadline submission
        secondary_pool = (
            self.deadline_pool_secondary or instance.data.get("secondaryPool")
        )
        payload = {
            "JobInfo": {
                "Plugin": "Ayon",
                "BatchName": job["Props"]["Batch"],
                "Name": job_name,
                "UserName": job["Props"]["User"],
                "Comment": instance.context.data.get("comment", ""),

                "Department": self.deadline_department,
                "ChunkSize": 1,
                "Priority": priority,
                "InitialStatus": initial_status,

                "Group": self.deadline_group,
                "Pool": self.deadline_pool or instance.data.get("primaryPool"),
                "SecondaryPool": secondary_pool,
                # ensure the outputdirectory with correct slashes
                "OutputDirectory0": output_dir.replace("\\", "/")
            },
            "PluginInfo": {
                "Version": self.plugin_pype_version,
                "Arguments": " ".join(args),
                "SingleFrameOnly": "True",
            },
            # Mandatory for Deadline, may be empty
            "AuxFiles": [],
        }

        # add assembly jobs as dependencies
        if instance.data.get("tileRendering"):
            self.log.info("Adding tile assembly jobs as dependencies...")
            job_index = 0
            for assembly_id in instance.data.get("assemblySubmissionJobs"):
                payload["JobInfo"]["JobDependency{}".format(
                    job_index)] = assembly_id  # noqa: E501
                job_index += 1
        elif instance.data.get("bakingSubmissionJobs"):
            self.log.info(
                "Adding baking submission jobs as dependencies..."
            )
            job_index = 0
            for assembly_id in instance.data["bakingSubmissionJobs"]:
                payload["JobInfo"]["JobDependency{}".format(
                    job_index)] = assembly_id  # noqa: E501
                job_index += 1
        elif job.get("_id"):
            payload["JobInfo"]["JobDependency0"] = job["_id"]

        for index, (key_, value_) in enumerate(environment.items()):
            payload["JobInfo"].update(
                {
                    "EnvironmentKeyValue%d"
                    % index: "{key}={value}".format(
                        key=key_, value=value_
                    )
                }
            )
        # remove secondary pool
        payload["JobInfo"].pop("SecondaryPool", None)

        self.log.debug("Submitting Deadline publish job ...")

        url = "{}/api/jobs".format(self.deadline_url)
        auth = instance.data["deadline"]["auth"]
        verify = instance.data["deadline"]["verify"]
        response = requests_post(
            url, json=payload, timeout=10, auth=auth, verify=verify)
        if not response.ok:
            raise Exception(response.text)

        deadline_publish_job_id = response.json()["_id"]

        return deadline_publish_job_id

    def process(self, instance):
        # type: (pyblish.api.Instance) -> None
        """Process plugin.

        Detect type of render farm submission and create and post dependent
        job in case of Deadline. It creates json file with metadata needed for
        publishing in directory of render.

        Args:
            instance (pyblish.api.Instance): Instance data.

        """
        if not instance.data.get("farm"):
            self.log.debug("Skipping local instance.")
            return

        anatomy = instance.context.data["anatomy"]

        instance_skeleton_data = create_skeleton_instance(
            instance, families_transfer=self.families_transfer,
            instance_transfer=self.instance_transfer)
        """
        if content of `expectedFiles` list are dictionaries, we will handle
        it as list of AOVs, creating instance for every one of them.

        Example:
        --------

        expectedFiles = [
            {
                "beauty": [
                    "foo_v01.0001.exr",
                    "foo_v01.0002.exr"
                ],

                "Z": [
                    "boo_v01.0001.exr",
                    "boo_v01.0002.exr"
                ]
            }
        ]

        This will create instances for `beauty` and `Z` product
        adding those files to their respective representations.

        If we have only list of files, we collect all file sequences.
        More then one doesn't probably make sense, but we'll handle it
        like creating one instance with multiple representations.

        Example:
        --------

        expectedFiles = [
            "foo_v01.0001.exr",
            "foo_v01.0002.exr",
            "xxx_v01.0001.exr",
            "xxx_v01.0002.exr"
        ]

        This will result in one instance with two representations:
        `foo` and `xxx`
        """
        do_not_add_review = False
        if instance.data.get("review") is False:
            self.log.debug("Instance has review explicitly disabled.")
            do_not_add_review = True

        aov_filter = {
            item["name"]: item["value"]
            for item in self.aov_filter
        }
        if isinstance(instance.data.get("expectedFiles")[0], dict):
            instances = create_instances_for_aov(
                instance, instance_skeleton_data,
                aov_filter,
                self.skip_integration_repre_list,
                do_not_add_review
            )
        else:
            representations = prepare_representations(
                instance_skeleton_data,
                instance.data.get("expectedFiles"),
                anatomy,
                aov_filter,
                self.skip_integration_repre_list,
                do_not_add_review,
                instance.context,
                self
            )

            if "representations" not in instance_skeleton_data.keys():
                instance_skeleton_data["representations"] = []

            # add representation
            instance_skeleton_data["representations"] += representations
            instances = [instance_skeleton_data]

        # attach instances to product
        if instance.data.get("attachTo"):
            instances = attach_instances_to_product(
                instance.data.get("attachTo"), instances
            )

        r''' SUBMiT PUBLiSH JOB 2 D34DLiN3
          ____
        '     '            .---.  .---. .--. .---. .--..--..--..--. .---.
        |     |   --= \   |  .  \/   _|/    \|  .  \  ||  ||   \  |/   _|
        | JOB |   --= /   |  |  ||  __|  ..  |  |  |  |;_ ||  \   ||  __|
        |     |           |____./ \.__|._||_.|___./|_____|||__|\__|\.___|
        ._____.

        '''

        render_job = instance.data.pop("deadlineSubmissionJob", None)
        if not render_job and instance.data.get("tileRendering") is False:
            raise AssertionError(("Cannot continue without valid "
                                  "Deadline submission."))
        if not render_job:
            import getpass

            render_job = {}
            self.log.debug("Faking job data ...")
            render_job["Props"] = {}
            # Render job doesn't exist because we do not have prior submission.
            # We still use data from it so lets fake it.
            #
            # Batch name reflect original scene name

            if instance.data.get("assemblySubmissionJobs"):
                render_job["Props"]["Batch"] = instance.data.get(
                    "jobBatchName")
            else:
                batch = os.path.splitext(os.path.basename(
                    instance.context.data.get("currentFile")))[0]
                render_job["Props"]["Batch"] = batch
            # User is deadline user
            render_job["Props"]["User"] = instance.context.data.get(
                "deadlineUser", getpass.getuser())

            render_job["Props"]["Env"] = {
                "FTRACK_API_USER": os.environ.get("FTRACK_API_USER"),
                "FTRACK_API_KEY": os.environ.get("FTRACK_API_KEY"),
                "FTRACK_SERVER": os.environ.get("FTRACK_SERVER"),
            }

        # get default deadline webservice url from deadline module
        self.deadline_url = instance.data["deadline"]["url"]
        assert self.deadline_url, "Requires Deadline Webservice URL"

        deadline_publish_job_id = \
            self._submit_deadline_post_job(instance, render_job, instances)

        # Inject deadline url to instances to query DL for job id for overrides
        for inst in instances:
            inst["deadline"] = instance.data["deadline"]

        # publish job file
        publish_job = {
            "folderPath": instance_skeleton_data["folderPath"],
            "frameStart": instance_skeleton_data["frameStart"],
            "frameEnd": instance_skeleton_data["frameEnd"],
            "fps": instance_skeleton_data["fps"],
            "source": instance_skeleton_data["source"],
            "user": instance.context.data["user"],
            "version": instance.context.data["version"],  # workfile version
            "intent": instance.context.data.get("intent"),
            "comment": instance.context.data.get("comment"),
            "job": render_job or None,
            "instances": instances
        }

        if deadline_publish_job_id:
            publish_job["deadline_publish_job_id"] = deadline_publish_job_id

        # add audio to metadata file if available
        audio_file = instance.context.data.get("audioFile")
        if audio_file and os.path.isfile(audio_file):
            publish_job.update({"audio": audio_file})

        metadata_path, rootless_metadata_path = \
            create_metadata_path(instance, anatomy)

        with open(metadata_path, "w") as f:
            json.dump(publish_job, f, indent=4, sort_keys=True)

    def _get_publish_folder(self, anatomy, template_data,
                            folder_entity, product_name, context,
                            product_type, version=None):
        """
            Extracted logic to pre-calculate real publish folder, which is
            calculated in IntegrateNew inside of Deadline process.
            This should match logic in:
                'collect_anatomy_instance_data' - to
                    get correct anatomy, family, version for product name and
                'collect_resources_path'
                    get publish_path

        Args:
            anatomy (ayon_core.pipeline.anatomy.Anatomy):
            template_data (dict): pre-calculated collected data for process
            folder_entity (dict[str, Any]): Folder entity.
            product_name (string): Product name (actually group name
                of product)
            product_type (string): for current deadline process it's always
                'render'
                TODO - for generic use family needs to be dynamically
                    calculated like IntegrateNew does
            version (int): override version from instance if exists

        Returns:
            (string): publish folder where rendered and published files will
                be stored
                based on 'publish' template
        """

        project_name = context.data["projectName"]
        host_name = context.data["hostName"]
        if not version:
            version_entity = None
            if folder_entity:
                version_entity = ayon_api.get_last_version_by_product_name(
                    project_name,
                    product_name,
                    folder_entity["id"]
                )

            if version_entity:
                version = int(version_entity["version"]) + 1
            else:
                version = get_versioning_start(
                    project_name,
                    host_name,
                    task_name=template_data["task"]["name"],
                    task_type=template_data["task"]["type"],
                    product_type="render",
                    product_name=product_name,
                    project_settings=context.data["project_settings"]
                )

        host_name = context.data["hostName"]
        task_info = template_data.get("task") or {}

        template_name = publish.get_publish_template_name(
            project_name,
            host_name,
            product_type,
            task_info.get("name"),
            task_info.get("type"),
        )

        template_data["version"] = version
        template_data["subset"] = product_name
        template_data["family"] = product_type
        template_data["product"] = {
            "name": product_name,
            "type": product_type,
        }

        render_dir_template = anatomy.get_template_item(
            "publish", template_name, "directory"
        )
        return render_dir_template.format_strict(template_data)

    @classmethod
    def get_attribute_defs(cls):
        return [
            EnumDef("publishJobState",
                    label="Publish Job State",
                    items=["Active", "Suspended"],
                    default="Active")
        ]
