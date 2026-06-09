# SPDX-License-Identifier: Apache-2.0
CC      ?= cc
CFLAGS  ?= -std=c11 -D_GNU_SOURCE -Wall -Wextra -O2
ROOT     = ..
INCLUDES = -I$(ROOT)/include -I.
# driver + the JSON/jsonrpc core (no NATS needed for these unit tests)
SRCS = $(ROOT)/src/json.c $(ROOT)/src/jsonrpc.c $(ROOT)/src/driver.c
TESTS = test_edge

all: $(TESTS)
test_edge: test_edge.c $(SRCS)
	$(CC) $(CFLAGS) $(INCLUDES) -o $@ $^ -lm
test: all
	@fail=0; for t in $(TESTS); do echo "== $$t =="; ./$$t || fail=1; done; exit $$fail
clean:
	rm -f $(TESTS)
.PHONY: all test clean
