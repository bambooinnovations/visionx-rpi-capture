import logging

import structlog
from structlog.processors import CallsiteParameter, CallsiteParameterAdder


def configure_logging(env: str, log_level: str = "INFO") -> None:
    """Configure structlog for the application.

    Dev:  coloured, human-readable console output with callsite info.
    Prod: JSON output — one log line per event, queryable by any aggregator.

    Stdlib loggers (uvicorn, SQLAlchemy, …) are routed through the same
    processor chain via ProcessorFormatter so all logs have a consistent shape.
    """

    # Processors that run on every log record regardless of environment.
    shared_processors: list = [
        # Merge any context set via structlog.contextvars.bind_contextvars()
        # (e.g. request_id bound in middleware flows through automatically).
        structlog.contextvars.merge_contextvars,
        # Capture the stdlib logger name so we can see which module logged.
        structlog.stdlib.add_logger_name,
        # Add the level name ("info", "error", …).
        structlog.stdlib.add_log_level,
        # ISO-8601 timestamp.
        structlog.processors.TimeStamper(fmt="iso"),
        # File name, function name, and line number of the log call site.
        # Disabled in prod to avoid the frame-inspection overhead on hot paths;
        # stack traces on exceptions (below) cover the prod debugging need.
        *(
            [
                CallsiteParameterAdder(
                    [
                        CallsiteParameter.FILENAME,
                        CallsiteParameter.FUNC_NAME,
                        CallsiteParameter.LINENO,
                    ]
                )
            ]
            if env == "dev"
            else []
        ),
        # Render exc_info / stack_info attached to a log call.
        structlog.processors.StackInfoRenderer(),
        # Format exception tuples into a string so they survive JSON serialisation.
        structlog.processors.ExceptionRenderer(),
    ]

    renderer = (
        structlog.dev.ConsoleRenderer()
        if env == "dev"
        else structlog.processors.JSONRenderer()
    )

    # Wire structlog up to emit through the stdlib handler below so that
    # uvicorn / third-party logs and our logs all share the same formatter.
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        # Do NOT cache when callsite params are active — caching freezes the
        # processor chain at a fixed frame depth, breaking file/line detection.
        cache_logger_on_first_use=env != "dev",
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # foreign_pre_chain handles records that originate from stdlib loggers
        # (uvicorn, SQLAlchemy, etc.) before the shared processors run.
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level.upper())
