# SPDX-License-Identifier: Apache-2.0
CC      ?= cc
CFLAGS  ?= -std=c11 -D_GNU_SOURCE -Wall -Wextra -O2
ROOT     = ..
CURL_CFLAGS ?=
CURL_LIBS   ?= -lcurl
INCLUDES = -I$(ROOT)/include -I. $(CURL_CFLAGS)
SRCS = $(ROOT)/src/json.c $(ROOT)/src/http.c $(ROOT)/src/agent_tools.c
TESTS = test_agent

all: $(TESTS)
test_agent: test_agent.c $(SRCS)
	$(CC) $(CFLAGS) $(INCLUDES) -o $@ $^ -lm $(CURL_LIBS)
test: all
	@fail=0; for t in $(TESTS); do echo "== $$t =="; ./$$t || fail=1; done; exit $$fail
clean:
	rm -f $(TESTS)
.PHONY: all test clean
