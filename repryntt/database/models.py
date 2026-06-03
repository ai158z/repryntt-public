#!/usr/bin/env python3
"""
Database Models for SAIGE
SQLAlchemy models for all persistent data
"""

from datetime import datetime
from typing import Dict, Any, List
from sqlalchemy import Column, Integer, String, Text, DateTime, Float, Boolean, JSON, ForeignKey, BigInteger
from sqlalchemy.orm import relationship, declarative_base
from sqlalchemy.dialects.postgresql import JSONB, ARRAY

from .config import Base

# ============================================================================
# ROBOT ECONOMY MODELS
# ============================================================================

class Block(Base):
    """Blockchain block model"""
    __tablename__ = "blocks"

    id = Column(Integer, primary_key=True, index=True)
    index = Column(BigInteger, unique=True, nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow)
    previous_hash = Column(String(128), nullable=False)
    hash = Column(String(128), nullable=False, unique=True, index=True)
    nonce = Column(BigInteger, nullable=False)
    difficulty = Column(BigInteger, nullable=False)
    miner_address = Column(String(64), nullable=False, index=True)
    reward_plancks = Column(BigInteger, nullable=False)
    transactions = Column(JSONB, nullable=False, default=list)  # List of transaction data
    merkle_root = Column(String(128), nullable=False)

    # Relationships
    transactions_rel = relationship("Transaction", back_populates="block", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Block(index={self.index}, hash={self.hash[:16]}...)>"


class Transaction(Base):
    """Blockchain transaction model"""
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    tx_hash = Column(String(128), unique=True, nullable=False, index=True)
    block_index = Column(BigInteger, ForeignKey("blocks.index"), nullable=False)
    sender = Column(String(64), nullable=False, index=True)
    recipient = Column(String(64), nullable=False, index=True)
    amount_plancks = Column(BigInteger, nullable=False)
    fee_plancks = Column(BigInteger, nullable=False, default=0)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow)
    signature = Column(Text, nullable=True)  # Optional signature data
    tx_type = Column(String(32), nullable=False, default="transfer")  # transfer, contract, etc.
    data = Column(JSONB, nullable=True)  # Additional transaction data

    # Relationships
    block = relationship("Block", back_populates="transactions_rel")

    def __repr__(self):
        return f"<Transaction(hash={self.tx_hash[:16]}..., amount={self.amount_plancks})>"


class Wallet(Base):
    """Wallet balance and information"""
    __tablename__ = "wallets"

    id = Column(Integer, primary_key=True, index=True)
    address = Column(String(64), unique=True, nullable=False, index=True)
    balance_plancks = Column(BigInteger, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_updated = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    wallet_type = Column(String(32), nullable=False, default="user")  # user, miner, submitter, ai
    is_active = Column(Boolean, nullable=False, default=True)
    extra_data = Column(JSONB, nullable=True)  # Additional wallet data

    def __repr__(self):
        return f"<Wallet(address={self.address[:16]}..., balance={self.balance_plancks})>"


class SmartContract(Base):
    """Smart contract/workload data"""
    __tablename__ = "smart_contracts"

    id = Column(Integer, primary_key=True, index=True)
    contract_key = Column(String(128), unique=True, nullable=False, index=True)
    machine_address = Column(String(64), nullable=False, index=True)
    purpose = Column(Text, nullable=False)
    status = Column(String(32), nullable=False, default="pending")  # pending, completed, failed
    data_hash = Column(String(128), nullable=False)
    storage_nodes = Column(ARRAY(String), nullable=False)
    fee_plancks = Column(BigInteger, nullable=False)
    reward_plancks = Column(BigInteger, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    result = Column(JSONB, nullable=True)  # Completion result data
    error_message = Column(Text, nullable=True)

    def __repr__(self):
        return f"<SmartContract(key={self.contract_key[:16]}..., status={self.status})>"


class DeploymentKey(Base):
    """Machine deployment keys"""
    __tablename__ = "deployment_keys"

    id = Column(Integer, primary_key=True, index=True)
    machine_address = Column(String(64), unique=True, nullable=False, index=True)
    deployment_key = Column(String(128), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    is_active = Column(Boolean, nullable=False, default=True)

    def __repr__(self):
        return f"<DeploymentKey(machine={self.machine_address[:16]}..., active={self.is_active})>"


# ============================================================================
# EXTERNAL API MODELS
# ============================================================================

class APIKey(Base):
    """API key management"""
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    api_key = Column(String(128), unique=True, nullable=False, index=True)
    user_id = Column(String(64), nullable=False, index=True)
    permissions = Column(ARRAY(String), nullable=False, default=["read"])
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_used = Column(DateTime, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    rate_limit_max = Column(Integer, nullable=False, default=100)
    rate_limit_window = Column(Integer, nullable=False, default=3600)  # seconds

    def __repr__(self):
        return f"<APIKey(user={self.user_id}, key={self.api_key[:16]}...)>"


class APIUsageLog(Base):
    """API usage logging for analytics"""
    __tablename__ = "api_usage_logs"

    id = Column(Integer, primary_key=True, index=True)
    api_key_id = Column(Integer, ForeignKey("api_keys.id"), nullable=False)
    endpoint = Column(String(256), nullable=False)
    method = Column(String(8), nullable=False)
    status_code = Column(Integer, nullable=False)
    response_time_ms = Column(Float, nullable=False)
    credits_used = Column(Float, nullable=False, default=0.0)
    ip_address = Column(String(45), nullable=True)  # IPv4/IPv6
    user_agent = Column(Text, nullable=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    api_key = relationship("APIKey")

    def __repr__(self):
        return f"<APIUsageLog(endpoint={self.endpoint}, status={self.status_code})>"


# ============================================================================
# BRAIN SYSTEM MODELS
# ============================================================================

class BrainMemory(Base):
    """Brain memory storage"""
    __tablename__ = "brain_memories"

    id = Column(Integer, primary_key=True, index=True)
    memory_id = Column(String(512), unique=True, nullable=False, index=True)
    memory_type = Column(String(32), nullable=False)  # episodic, semantic, procedural, working
    content = Column(JSONB, nullable=False)
    importance = Column(Float, nullable=False, default=0.5)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_accessed = Column(DateTime, nullable=False, default=datetime.utcnow)
    access_count = Column(Integer, nullable=False, default=0)
    tags = Column(ARRAY(String), nullable=True)

    def __repr__(self):
        return f"<BrainMemory(id={self.memory_id[:16]}..., type={self.memory_type})>"


class ChainOfThought(Base):
    """Chain of thought reasoning data"""
    __tablename__ = "chains_of_thought"

    id = Column(Integer, primary_key=True, index=True)
    chain_id = Column(String(128), unique=True, nullable=False, index=True)
    topic = Column(String(256), nullable=False)
    goal = Column(Text, nullable=False)
    status = Column(String(32), nullable=False, default="active")  # active, completed, paused
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    success_criteria = Column(ARRAY(String), nullable=True)
    current_phase = Column(String(64), nullable=True)
    extra_data = Column(JSONB, nullable=True)

    # Relationships
    steps = relationship("ChainStep", back_populates="chain", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<ChainOfThought(id={self.chain_id[:16]}..., status={self.status})>"


class ChainStep(Base):
    """Individual steps in a chain of thought"""
    __tablename__ = "chain_steps"

    id = Column(Integer, primary_key=True, index=True)
    chain_id = Column(String(128), ForeignKey("chains_of_thought.chain_id"), nullable=False)
    step_number = Column(Integer, nullable=False)
    phase = Column(String(64), nullable=False)
    prompt = Column(Text, nullable=False)
    response = Column(Text, nullable=True)
    insights = Column(ARRAY(String), nullable=True)
    next_questions = Column(ARRAY(String), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    processing_time = Column(Float, nullable=True)  # seconds

    # Relationships
    chain = relationship("ChainOfThought", back_populates="steps")

    def __repr__(self):
        return f"<ChainStep(chain={self.chain_id[:16]}..., step={self.step_number})>"


class PersonalityEvolution(Base):
    """AI personality evolution tracking"""
    __tablename__ = "personality_evolution"

    id = Column(Integer, primary_key=True, index=True)
    evolution_id = Column(String(128), unique=True, nullable=False, index=True)
    personality_data = Column(JSONB, nullable=False)
    hormone_levels = Column(JSONB, nullable=False)
    qlora_metrics = Column(JSONB, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    trigger_reason = Column(String(256), nullable=True)
    success_rating = Column(Float, nullable=True)  # 0.0 to 1.0

    def __repr__(self):
        return f"<PersonalityEvolution(id={self.evolution_id[:16]}..., rating={self.success_rating})>"


# ============================================================================
# SYSTEM METRICS MODELS
# ============================================================================

class SystemMetric(Base):
    """System performance metrics"""
    __tablename__ = "system_metrics"

    id = Column(Integer, primary_key=True, index=True)
    metric_name = Column(String(128), nullable=False, index=True)
    metric_value = Column(Float, nullable=False)
    metric_type = Column(String(32), nullable=False)  # counter, gauge, histogram
    labels = Column(JSONB, nullable=True)  # Additional labels/tags
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f"<SystemMetric(name={self.metric_name}, value={self.metric_value})>"


class ServiceHealth(Base):
    """Service health monitoring"""
    __tablename__ = "service_health"

    id = Column(Integer, primary_key=True, index=True)
    service_name = Column(String(128), nullable=False, index=True)
    service_type = Column(String(64), nullable=False)  # ai_server, api, brain_system, etc.
    status = Column(String(32), nullable=False)  # healthy, degraded, unhealthy
    response_time = Column(Float, nullable=True)  # milliseconds
    last_check = Column(DateTime, nullable=False, default=datetime.utcnow)
    next_check = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    extra_data = Column(JSONB, nullable=True)

    def __repr__(self):
        return f"<ServiceHealth(name={self.service_name}, status={self.status})>"
