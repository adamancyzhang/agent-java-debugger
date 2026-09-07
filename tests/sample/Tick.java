/**
 * Minimal target app for ajd e2e tests.  Ticks every 500 ms, exercising:
 *   - breakpoints in a static method (main) and an inner class (Tick$Work)
 *   - locals of primitive/object types, `this`, string concat
 *   - a counter that grows predictably, for deterministic conditions
 */
public class Tick {
    static volatile boolean running = true;
    static int counter = 0;

    public static void main(String[] args) throws Exception {
        System.out.println("Tick started");
        Work work = new Work("w1"); Thread.sleep(800); // sleep AFTER Work loads so pending breakpoints arm before tick #0
        int i = 0;
        while (running) {
            i = work.tick(i);                       // line 16
            Thread.sleep(500);
        }
        System.out.println("Tick stopped i=" + i);
    }

    static class Work {
        final String name;
        int total;

        Work(String name) { this.name = name; }

        int tick(int i) {
            int doubled = i * 2 + 1;                // line 29
            String msg = name + "#" + doubled;      // line 30
            java.util.List<String> history = new java.util.ArrayList<>();
            history.add(msg);                       // line 32
            counter++;                              // line 33
            total = doubled;                        // line 34
            System.out.println("tick " + msg);      // line 35
            return i + 1;                           // line 36
        }
    }
}
