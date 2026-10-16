// Host test for elapsed() in weather_node.ino: g++ -std=c++11 -o /tmp/t debug/tests/test_elapsed.cpp && /tmp/t
#include <cstdint>
#include <cstdio>
// copied from weather_node.ino
uint32_t elapsed(uint32_t since, uint32_t now) {
  int32_t d = (int32_t)(now - since);
  return d > 0 ? (uint32_t)d : 0;
}
int main() {
  const uint32_t timeout = 4000;
  struct { const char *name; uint32_t now, lastRx; bool wantLost; } cases[] = {
    {"packet stamped 1 ms after loop read the clock", 1000, 1001, false},
    {"packet stamped 9 ms after (slow serial print)",  1000, 1009, false},
    {"packet 1 s ago",                                 5000, 4000, false},
    {"packet 4.001 s ago (real timeout)",              8001, 4000, true},
    {"millis() wrapped, packet 10 ms ago",             5, 0xFFFFFFFBu, false},
    {"millis() wrapped, packet 5 s ago",               4996, 0xFFFFFFFCu, true},
  };
  int fails = 0;
  for (auto &c : cases) {
    bool oldLost = c.now - c.lastRx > timeout;
    bool newLost = elapsed(c.lastRx, c.now) > timeout;
    bool ok = newLost == c.wantLost;
    fails += !ok;
    printf("%-48s old code: %-9s fixed: %-9s %s\n", c.name, oldLost ? "LOST" : "up", newLost ? "LOST" : "up", ok ? "PASS" : "FAIL");
  }
  return fails;
}
