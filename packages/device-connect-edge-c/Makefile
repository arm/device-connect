# SPDX-License-Identifier: Apache-2.0
# Device Connect C edge SDK.
#
# Builds libdc_edge.a (the device-side library) and the temp_sensor example.
# Requires the NATS C client (cnats); point NATS_CFLAGS/NATS_LIBS at it.
#
#   make                     # build libdc_edge.a + temp_sensor
#   make test                # build and run unit tests
#   make NATS_CFLAGS=-I/opt/homebrew/include NATS_LIBS="-L/opt/homebrew/lib -lnats"
#   make clean

CC      ?= cc
CFLAGS  ?= -std=c11 -D_GNU_SOURCE -Wall -Wextra -O2
NATS_CFLAGS ?=
NATS_LIBS   ?= -lnats
DEFS     = -DDC_WITH_NATS
INCLUDES = -Iinclude $(NATS_CFLAGS)
LDLIBS   = $(NATS_LIBS) -lm

LIB  = libdc_edge.a
SRCS = src/json.c src/hash.c src/jsonrpc.c src/security.c \
       src/transport_nats.c src/driver.c src/runtime.c
OBJS = $(SRCS:.c=.o)

all: $(LIB) temp_sensor

$(LIB): $(OBJS)
	ar rcs $@ $^

src/%.o: src/%.c
	$(CC) $(CFLAGS) $(DEFS) $(INCLUDES) -c $< -o $@

temp_sensor: examples/temp_sensor.c $(LIB)
	$(CC) $(CFLAGS) $(DEFS) $(INCLUDES) -o $@ examples/temp_sensor.c $(LIB) $(LDLIBS)

test: $(LIB)
	$(MAKE) -C tests test

clean:
	rm -f $(OBJS) $(LIB) temp_sensor
	$(MAKE) -C tests clean 2>/dev/null || true

.PHONY: all test clean
