# Multi-stage build for EnergyPlus validator container

# =============================================================================
# Stage 1: Build EnergyPlus
# =============================================================================
FROM ubuntu:22.04 AS energyplus-builder

# Install EnergyPlus build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Download and install EnergyPlus 24.2.0
# Update this URL for different EnergyPlus versions
RUN wget -q https://github.com/NREL/EnergyPlus/releases/download/v24.2.0/EnergyPlus-24.2.0-9f1b36b00b-Linux-Ubuntu22.04-x86_64.tar.gz \
    && tar -xzf EnergyPlus-24.2.0-9f1b36b00b-Linux-Ubuntu22.04-x86_64.tar.gz \
    && mv EnergyPlus-24.2.0-9f1b36b00b-Linux-Ubuntu22.04-x86_64 /opt/energyplus \
    && rm EnergyPlus-24.2.0-9f1b36b00b-Linux-Ubuntu22.04-x86_64.tar.gz

# =============================================================================
# Stage 2: Runtime container
# =============================================================================
FROM python:3.13-slim

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy EnergyPlus from builder stage
COPY --from=energyplus-builder /opt/energyplus /opt/energyplus

# Add EnergyPlus to PATH
ENV PATH="/opt/energyplus:${PATH}"

# Set working directory
WORKDIR /app

# Copy requirements first (for layer caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared utilities
COPY ../shared /app/validators/shared

# Copy validator code
COPY . /app/validators/energyplus

# Create working directory for EnergyPlus runs
RUN mkdir -p /tmp/energyplus_run

# Set entrypoint
WORKDIR /app/validators/energyplus
CMD ["python", "main.py"]
