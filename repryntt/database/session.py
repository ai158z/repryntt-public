#!/usr/bin/env python3
"""
Database Session Management
Provides database session handling with proper cleanup
"""

from contextlib import contextmanager
from typing import Generator
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
import logging

from .config import db_config

logger = logging.getLogger(__name__)

def get_session() -> Session:
    """Get a database session"""
    return db_config.get_session()

@contextmanager
def get_db_session() -> Generator[Session, None, None]:
    """Context manager for database sessions with automatic cleanup"""
    session = None
    try:
        session = get_session()
        yield session
    except Exception as e:
        if session:
            session.rollback()
        logger.error(f"Database session error: {e}")
        raise
    finally:
        if session:
            session.close()

def init_database():
    """Initialize database and create all tables"""
    try:
        engine = db_config.create_engine()

        # Import all models to ensure they are registered with SQLAlchemy
        from . import models

        # Create all tables
        models.Base.metadata.create_all(bind=engine)

        logger.info("✅ Database initialized successfully")
        return True

    except Exception as e:
        logger.error(f"❌ Database initialization failed: {e}")
        return False

def health_check() -> bool:
    """Check database connectivity"""
    try:
        with get_db_session() as session:
            session.execute("SELECT 1")
        return True
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        return False
