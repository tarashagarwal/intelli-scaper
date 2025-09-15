SHELL := /bin/bash

# Default target
all: install-docker

# Update package lists
update:
	sudo apt-get update -y

# Install required dependencies
deps:
	sudo apt-get install -y \
		ca-certificates \
		curl \
		gnupg \
		lsb-release

# Add Docker’s official GPG key and repo
repo:
	sudo mkdir -p /etc/apt/keyrings
	curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
	echo \
	  "deb [arch=$$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
	  $$(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker
install-docker: update deps repo
	sudo apt-get update -y
	sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# Verify installation
verify:
	docker --version
	docker compose version


#playwright install
#playwright install-deps
