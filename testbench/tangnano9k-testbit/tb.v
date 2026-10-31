// Simulation: iverilog -o build/tb tb.v top.v && vvp build/tb
// Runs the design with a 1.152 MHz clock (UART divider 10) so 1 s ticks are cheap.
`timescale 1ns/1ns
module tb;
    localparam HZ = 1_152_000, BAUD = 115_200, DIV = HZ / BAUD, BIT = DIV * 10; // ns per bit at 10 ns clk
    reg clk = 0; always #5 clk = ~clk;
    reg rx = 1, b1 = 1, b2 = 1;
    wire tx; wire [5:0] led_n;
    top #(.CLK_HZ(HZ), .BAUD(BAUD)) dut(.clk(clk), .btn1_n(b1), .btn2_n(b2), .uart_rx(rx), .uart_tx(tx), .led_n(led_n));

    // decode TX and print what the host would see
    integer i; reg [7:0] c;
    initial forever begin
        @(negedge tx); #(BIT/2);
        for (i = 0; i < 8; i = i + 1) begin #(BIT); c[i] = tx; end
        #(BIT);
        if (!tx) $display("!! framing error");
        if (c == 8'h0d) ; else if (c == 8'h0a) $write("\n"); else $write("%c", c);
    end

    task send(input [7:0] b); integer k; begin
        rx = 0; #(BIT);
        for (k = 0; k < 8; k = k + 1) begin rx = b[k]; #(BIT); end
        rx = 1; #(BIT * 2);
    end endtask

    initial begin
        #(1_200_000_000);          // 1.2 s: banner + first hb
        send("?");  #(10_000_000);
        send("x");  #(1_000_000); $write("\n");
        send("l");  #(10_000_000);
        b2 = 0; #(20_000_000); b2 = 1; #(10_000_000);
        send("h");  #(1_500_000_000);  // hb off: no hb for 1.5 s
        send("h");  #(1_200_000_000);  // hb on again
        $display("--- led_n=%b", led_n);
        $finish;
    end
endmodule
