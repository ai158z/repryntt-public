#!/usr/bin/env python3
"""
Database Configuration for SAIGE
PostgreSQL connection and settings management
"""

import os
from typing import Optional
from sqlalchemy.engine import URL
from sqlalchemy import create_engine, MetaData
from sqlalchemy.orm import sessionmaker, declarative_base

# SQLAlchemy Base for all models
Base = declarative_base()
metadata = MetaData()

class DatabaseConfig:
    """Database configuration and connection management"""

    def __init__(self):
        # Database connection settings
        self.host = os.environ.get('SAIGE_DB_HOST', 'localhost')
        self.port = int(os.environ.get('SAIGE_DB_PORT', '5432'))
        self.database = os.environ.get('SAIGE_DB_NAME', 'saige_db')
        self.username = os.environ.get('SAIGE_DB_USER', 'saige')
        self.password = os.environ.get('SAIGE_DB_PASSWORD', 'saige_password')

        # Connection pool settings
        self.pool_size = int(os.environ.get('SAIGE_DB_POOL_SIZE', '10'))
        self.max_overflow = int(os.environ.get('SAIGE_DB_MAX_OVERFLOW', '20'))
        self.pool_timeout = int(os.environ.get('SAIGE_DB_POOL_TIMEOUT', '30'))
        self.pool_recycle = int(os.environ.get('SAIGE_DB_POOL_RECYCLE', '3600'))

        # Engine and session
        self.engine = None
        self.SessionLocal = None

    def get_database_url(self) -> str:
        """Generate PostgreSQL connection URL"""
        return URL.create(
            drivername="postgresql+psycopg2",
            username=self.username,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.database
        )

    def create_engine(self):
        """Create SQLAlchemy engine with optimized settings"""
        database_url = self.get_database_url()

        self.engine = create_engine(
            database_url,
            pool_size=self.pool_size,
            max_overflow=self.max_overflow,
            pool_timeout=self.pool_timeout,
            pool_recycle=self.pool_recycle,
            pool_pre_ping=True,  # Verify connections before use
            echo=False  # Set to True for SQL debugging
        )

        # Create session factory
        self.SessionLocal = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=self.engine
        )

        return self.engine

    def get_session(self):
        """Get a database session"""
        if not self.SessionLocal:
            self.create_engine()
        return self.SessionLocal()

# Global database configuration instance
db_config = DatabaseConfig()
