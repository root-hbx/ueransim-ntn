FROM ubuntu:jammy AS builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        make g++ libsctp-dev lksctp-tools \
        cmake libssl-dev ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /UERANSIM

COPY CMakeLists.txt makefile ./
COPY src/ ./src/
COPY tools/ ./tools/

RUN make build

FROM ubuntu:jammy

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libsctp1 lksctp-tools iproute2 iptables \
        iputils-ping net-tools procps && \
    apt-get autoremove -y && apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /UERANSIM
COPY --from=builder /UERANSIM/build /UERANSIM/build

WORKDIR /UERANSIM/build
