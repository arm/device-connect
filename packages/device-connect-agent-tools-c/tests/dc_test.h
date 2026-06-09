/* SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
 * dc_test.h -- minimal self-contained test harness. ASCII-only. */
#ifndef DC_TEST_H
#define DC_TEST_H
#include <stdio.h>
#include <string.h>
static int g_run, g_failed; static const char *g_t;
#define CHECK(e) do{ if(!(e)){ fprintf(stderr,"  FAIL %s:%d: %s\n",__FILE__,__LINE__,#e); g_failed++; } }while(0)
#define CHECK_STR(a,b) do{ const char*_a=(a),*_b=(b); if(!_a||!_b||strcmp(_a,_b)){ fprintf(stderr,"  FAIL %s:%d: \"%s\"!=\"%s\"\n",__FILE__,__LINE__,_a?_a:"(null)",_b?_b:"(null)"); g_failed++; } }while(0)
#define RUN(fn) do{ g_t=#fn; int b=g_failed; g_run++; fn(); fprintf(stderr,"  %s %s\n",(g_failed==b)?"ok  ":"FAIL",#fn);}while(0)
#define REPORT() do{ fprintf(stderr,"%d tests, %d failures\n",g_run,g_failed); return g_failed?1:0;}while(0)
#endif
