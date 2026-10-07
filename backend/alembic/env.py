from sqlalchemy import create_engine, pool

from alembic import context
from app import models  # noqa: F401 -- registers future model metadata
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.base import Base
from app.db.base import Base
import app.models  # Чтобы модель попала в Base.metadata

target_metadata = Base.metadata

configure_logging(get_settings().log_level)
target_metadata = Base.metadata
url = get_settings().database_url.get_secret_value()

if context.is_offline_mode():
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(
        url, poolclass=pool.NullPool, hide_parameters=True, connect_args={"connect_timeout": 5}
    )
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
