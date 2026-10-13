# SPDX-License-Identifier: Apache-2.0
# Device Connect C agent-tools.
#
# Builds libdc_agent_tools.a and the dc_agent CLI. Requires libcurl.
#
#   make
#   make CURL_CFLAGS=... CURL_LIBS=-lcurl
#   make test
#   make clean

CC      ?= cc
CFLAGS  ?= -std=c11 -D_GNU_SOURCE -Wall -Wextra -O2
CURL_CFLAGS ?=
CURL_LIBS   ?= -lcurl
INCLUDES = -Iinclude $(CURL_CFLAGS)
LDLIBS   = $(CURL_LIBS)

LIB  = libdc_agent_tools.a
SRCS = src/json.c src/http.c src/agent_tools.c
OBJS = $(SRCS:.c=.o)

all: $(LIB) dc_agent

$(LIB): $(OBJS)
	ar rcs $@ $^

src/%.o: src/%.c
	$(CC) $(CFLAGS) $(INCLUDES) -c $< -o $@

dc_agent: examples/dc_agent.c $(LIB)
	$(CC) $(CFLAGS) $(INCLUDES) -o $@ examples/dc_agent.c $(LIB) $(LDLIBS)

test: $(LIB)
	$(MAKE) -C tests test

clean:
	rm -f $(OBJS) $(LIB) dc_agent
	$(MAKE) -C tests clean 2>/dev/null || true

.PHONY: all test clean
