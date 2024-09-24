from typing import Any, Dict, List, Optional, Union

import bentoml
from bentoml import Tag
from bentoml.client import Client
from zenml.constants import DEFAULT_LOCAL_SERVICE_IP_ADDRESS
from zenml.client import Client as ZenMLClient
from zenml.integrations.bentoml.constants import BENTOML_DEFAULT_PORT, BENTOML_HEALTHCHECK_URL_PATH, BENTOML_PREDICTION_URL_PATH
from zenml.logger import get_logger
from zenml.services.container.container_service import ContainerService, ContainerServiceConfig
from zenml.services.container.container_service_endpoint import ContainerServiceEndpoint, ContainerServiceEndpointConfig
from zenml.services.service import BaseDeploymentService
from zenml.services.service_endpoint import ServiceEndpointProtocol
from zenml.services.service_monitor import HTTPEndpointHealthMonitor, HTTPEndpointHealthMonitorConfig
from zenml.services.service_type import ServiceType


logger = get_logger(__name__)

class BentoMLContainerDeploymentConfig(ContainerServiceConfig):
    """BentoML container deployment configuration."""
    model_name: str
    model_uri: str
    bento_tag: str
    bento_uri: Optional[str] = None
    platform: Optional[str] = None
    image: Optional[str] = None
    image_tag: Optional[str] = None
    features: Optional[List[str]] = None
    file: Optional[str] = None
    apis: List[str] = []
    workers: int = 1
    backlog: int = 2048
    host: Optional[str] = None
    port: Optional[int] = None


class BentoMLContainerDeploymentEndpointConfig(ContainerServiceEndpointConfig):
    """BentoML container deployment service configuration.

    Attributes:
        prediction_url_path: URI subpath for prediction requests
    """

    prediction_url_path: str


class BentoMLContainerDeploymentEndpoint(ContainerServiceEndpoint):
    """A service endpoint exposed by the BentoML container deployment service.

    Attributes:
        config: service endpoint configuration
    """

    config: BentoMLContainerDeploymentEndpointConfig

    @property
    def prediction_url(self) -> Optional[str]:
        """Gets the prediction URL for the endpoint.

        Returns:
            the prediction URL for the endpoint
        """
        uri = self.status.uri
        if not uri:
            return None


class BentoMLContainerDeploymentService(ContainerService, BaseDeploymentService):
    """BentoML container deployment service."""

    SERVICE_TYPE = ServiceType(
        name="bentoml-container-deployment",
        type="model-serving",
        flavor="bentoml",
        description="BentoML container prediction service",
        logo_url="https://public-flavor-logos.s3.eu-central-1.amazonaws.com/model_deployer/bentoml.png",
    )

    config: BentoMLContainerDeploymentConfig
    endpoint: BentoMLContainerDeploymentEndpoint

    def __init__(
        self,
        config: Union[BentoMLContainerDeploymentConfig, Dict[str, Any]],
        **attrs: Any,
    ) -> None:
        """Initialize the BentoML deployment service.

        Args:
            config: service configuration
            attrs: additional attributes to set on the service
        """
        # ensure that the endpoint is created before the service is initialized
        # TODO [ENG-700]: implement a service factory or builder for BentoML
        #   deployment services
        if (
            isinstance(config, BentoMLContainerDeploymentConfig)
            and "endpoint" not in attrs
        ):
            endpoint = BentoMLContainerDeploymentEndpoint(
                config=BentoMLContainerDeploymentEndpointConfig(
                    protocol=ServiceEndpointProtocol.HTTP,
                    port=config.port or BENTOML_DEFAULT_PORT,
                    ip_address=config.host or DEFAULT_LOCAL_SERVICE_IP_ADDRESS,
                    prediction_url_path=BENTOML_PREDICTION_URL_PATH,
                ),
                monitor=HTTPEndpointHealthMonitor(
                    config=HTTPEndpointHealthMonitorConfig(
                        healthcheck_uri_path=BENTOML_HEALTHCHECK_URL_PATH,
                    )
                ),
            )
            attrs["endpoint"] = endpoint
        super().__init__(config=config, **attrs)

    def _containerize_and_push_bento(self) -> None:
        """Containerize the bento and push it to the container registry."""
        zenml_client = ZenMLClient()
        container_registry = zenml_client.active_stack.container_registry
        # a tuple of config image and image tag
        if self.config.image and self.config.image_tag:
            image_tag = (self.config.image, self.config.image_tag)
        else:
            # if container registry is present in the stack, name the image
            # with the container registry uri, else name the image with the bento tag
            if container_registry:
                image_name = f"{container_registry.config.uri}/{self.config.bento_tag}"
                image_tag = (image_name,)
                self.config.image = image_name
            else:
                # bentoml will use the bento tag as the name of the image
                image_tag = (self.config.bento_tag,)
                self.config.image = self.config.bento_tag
        try:
            bentoml.container.build(
                bento_tag=self.config.bento_tag,
                backend="docker",  # hardcoding docker since container service only supports docker
                image_tag=image_tag,
                features=self.config.features,
                file=self.config.file,
                platform=self.config.platform,
            )

        except Exception as e:
            logger.error(f"Error containerizing the bento: {e}")
            raise e
        
        if container_registry:
            logger.info(f"Pushing bento to container registry {container_registry.config.uri}")
            # push the bento to the image registry
            container_registry.push_image(self.config.image)
        else:
            logger.warning("No container registry found in the active stack. "
                           "Please add a container registry to your stack to push "
                           "the bento to an image registry.")
        

    def provision(self) -> None:
        """Provision the service."""
        # containerize the bento
        self._containerize_and_push_bento()
        # run the container
        super().provision() 

    def run(self) -> None:
        """Start the service."""
        from bentoml import Service
        from bentoml._internal.service.loader import load

        logger.info("Starting BentoML container deployment service...")

        self.endpoint.prepare_for_start()

        svc = load(bento_identifier=self.config.bento_tag, working_dir=self.config.working_dir or '.')
        if isinstance(svc, Service):
            # bentoml<1.2
            from bentoml.serving import serve_http_production

            try:
                serve_http_production(
                    self.config.bento_tag,
                    port=self.endpoint.status.port,
                    backlog=self.config.backlog,
                    host=self.endpoint.status.hostname,
                )
            except Exception as e:
                logger.error(f"Error starting BentoML container deployment service: {e}")
                raise e
        else:
            # bentoml>=1.2
            from _bentoml_impl.server import serve_http

            svc.inject_config()
            try:
                serve_http(
                    self.config.bento_tag,
                    working_dir=self.config.working_dir or '.',
                    port=self.endpoint.status.port,
                    backlog=self.config.backlog,
                    host=self.endpoint.status.hostname,
                )
            except Exception as e:
                logger.error(f"Error starting BentoML container deployment service: {e}")
                raise e

    @property
    def prediction_url(self) -> Optional[str]:
        """Get the URI where the http server is running.

        Returns:
            The URI where the http service can be accessed to get more information
            about the service and to make predictions.
        """
        if not self.is_running:
            return None
        return self.endpoint.prediction_url
    
    @property
    def prediction_apis_urls(self) -> Optional[List[str]]:
        """Get the URI where the prediction api services is answering requests.

        Returns:
            The URI where the prediction service apis can be contacted to process
            HTTP/REST inference requests, or None, if the service isn't running.
        """
        if not self.is_running:
            return None

        if self.config.apis:
            return [
                f"{self.endpoint.prediction_url}/{api}"
                for api in self.config.apis
            ]
        return None
    

    def predict(self, api_endpoint: str, data: Any) -> Any:
        """Make a prediction using the service.

        Args:
            data: data to make a prediction on
            api_endpoint: the api endpoint to make the prediction on

        Returns:
            The prediction result.

        Raises:
            Exception: if the service is not running
            ValueError: if the prediction endpoint is unknown.
        """
        if not self.is_running:
            raise Exception(
                "BentoML prediction service is not running. "
                "Please start the service before making predictions."
            )
        if self.endpoint.prediction_url is not None:
            client = Client.from_url(
                self.endpoint.prediction_url.replace("http://", "").rstrip("/")
            )
            result = client.call(api_endpoint, data)
        else:
            raise ValueError("No endpoint known for prediction.")
        return result
        