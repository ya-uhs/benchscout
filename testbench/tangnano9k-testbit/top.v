// benchscout test bitstream for Sipeed Tang Nano 9K (GW1NR-9C).
//
// Gives the bench something known to observe on the fabric UART (BL702 bridge, 115200 8N1):
//   at start:   "benchscout-testbit tangnano9k v1 baud=115200\r\n"
//   every 1 s:  "hb XXXXXXXX\r\n"            (hex seconds since configuration; off with 'h')
//   buttons:    "btn S1\r\n" / "btn S2\r\n"  (on press)
//   rx '?'/'i': banner again
//   rx 'h':     toggle heartbeat, answers "hb on\r\n" / "hb off\r\n"
//   rx 'l':     next LED mode (0 counter, 1 chaser, 2 off), answers "led N\r\n"
//   rx other:   the byte is echoed back unchanged
module top #(
    parameter CLK_HZ = 27_000_000,   // overridden only by the testbench
    parameter BAUD   = 115_200
) (
    input  wire       clk,       // 27 MHz
    input  wire       btn1_n,    // S1, active low
    input  wire       btn2_n,    // S2, active low
    input  wire       uart_rx,
    output wire       uart_tx,
    output wire [5:0] led_n      // active low
);
    localparam DIV    = CLK_HZ / BAUD;   // 234

    // power-on reset
    reg [7:0] por = 0;
    wire rst = ~&por;
    always @(posedge clk) if (rst) por <= por + 1'b1;

    // 1 s tick and seconds counter
    reg [24:0] sub = 0;
    reg [31:0] secs = 0;
    reg tick = 0;
    always @(posedge clk) begin
        tick <= 0;
        if (sub == CLK_HZ - 1) begin sub <= 0; secs <= secs + 1; tick <= 1; end
        else sub <= sub + 1'b1;
    end

    // ---------------- UART TX ----------------
    reg [9:0] tx_sh = 10'h3ff;
    reg [3:0] tx_bits = 0;
    reg [7:0] tx_cnt = 0;
    reg tx_start = 0;
    reg [7:0] tx_data = 0;
    wire tx_busy = tx_bits != 0;
    assign uart_tx = tx_sh[0];
    always @(posedge clk) begin
        if (rst) begin tx_sh <= 10'h3ff; tx_bits <= 0; end
        else if (tx_start && !tx_busy) begin
            tx_sh <= {1'b1, tx_data, 1'b0}; tx_bits <= 10; tx_cnt <= 0;
        end else if (tx_busy) begin
            if (tx_cnt == DIV - 1) begin
                tx_cnt <= 0; tx_sh <= {1'b1, tx_sh[9:1]}; tx_bits <= tx_bits - 1'b1;
            end else tx_cnt <= tx_cnt + 1'b1;
        end
    end

    // ---------------- UART RX ----------------
    reg [2:0] rx_sync = 3'b111;
    always @(posedge clk) rx_sync <= {rx_sync[1:0], uart_rx};
    wire rxd = rx_sync[2];
    reg [8:0] rx_cnt = 0;
    reg [3:0] rx_bit = 0;
    reg [7:0] rx_sh = 0;
    reg rx_busy = 0;
    reg rx_valid = 0;
    reg [7:0] rx_byte = 0;
    always @(posedge clk) begin
        rx_valid <= 0;
        if (!rx_busy) begin
            if (!rxd) begin rx_busy <= 1; rx_cnt <= DIV / 2; rx_bit <= 0; end
        end else if (rx_cnt == DIV - 1) begin
            rx_cnt <= 0;
            rx_bit <= rx_bit + 1'b1;
            if (rx_bit == 0) begin
                if (rxd) rx_busy <= 0;                    // false start
            end else if (rx_bit <= 8) rx_sh <= {rxd, rx_sh[7:1]};
            else begin
                rx_busy <= 0;
                if (rxd) begin rx_valid <= 1; rx_byte <= rx_sh; end   // good stop bit
            end
        end else rx_cnt <= rx_cnt + 1'b1;
    end

    // ---------------- buttons (debounced, press events) ----------------
    reg [1:0] b_s0 = 2'b11, b_s1 = 2'b11, b_st = 2'b11;
    reg [17:0] b_t = 0;   // ~9.7 ms sample period
    reg [1:0] b_press = 0;
    always @(posedge clk) begin
        b_s0 <= {btn2_n, btn1_n}; b_s1 <= b_s0;
        b_press <= 0;
        b_t <= b_t + 1'b1;
        if (b_t == 0) begin
            b_st <= b_s1;
            b_press <= b_st & ~b_s1;   // 1 -> 0 = pressed
        end
    end

    // ---------------- message sequencer ----------------
    localparam M_BANNER = 3'd0, M_HB = 3'd1, M_S1 = 3'd2, M_S2 = 3'd3,
               M_ECHO = 3'd4, M_HBSTATE = 3'd5, M_LED = 3'd6;

    localparam BANNER_LEN = 46;
    localparam [8*BANNER_LEN-1:0] BANNER = "benchscout-testbit tangnano9k v1 baud=115200\r\n";

    function [7:0] hex(input [3:0] n);
        hex = n < 10 ? 8'd48 + n : 8'd87 + n;   // '0'..'9', 'a'..'f'
    endfunction

    reg hb_en = 1;
    reg [1:0] led_mode = 0;
    reg [31:0] cap = 0;       // value captured for the hb line
    reg [7:0] echo_c = 0;

    // byte i of message m; 0 marks the end
    function [7:0] msg_byte(input [2:0] m, input [5:0] i);
        begin
            msg_byte = 0;
            case (m)
                M_BANNER: if (i < BANNER_LEN) msg_byte = BANNER[8*(BANNER_LEN-1-i) +: 8];
                M_HB: case (i)
                    0: msg_byte = "h"; 1: msg_byte = "b"; 2: msg_byte = " ";
                    3,4,5,6,7,8,9,10: msg_byte = hex(cap[4*(10-i) +: 4]);
                    11: msg_byte = "\r"; 12: msg_byte = "\n";
                    default: msg_byte = 0;
                endcase
                M_S1, M_S2: case (i)
                    0: msg_byte = "b"; 1: msg_byte = "t"; 2: msg_byte = "n"; 3: msg_byte = " ";
                    4: msg_byte = "S"; 5: msg_byte = (m == M_S1) ? "1" : "2";
                    6: msg_byte = "\r"; 7: msg_byte = "\n";
                    default: msg_byte = 0;
                endcase
                M_ECHO: msg_byte = (i == 0) ? echo_c : 8'd0;
                M_HBSTATE: case (i)
                    0: msg_byte = "h"; 1: msg_byte = "b"; 2: msg_byte = " "; 3: msg_byte = "o";
                    4: msg_byte = hb_en ? "n" : "f";
                    5: msg_byte = hb_en ? "\r" : "f";
                    6: msg_byte = hb_en ? "\n" : "\r";
                    7: msg_byte = hb_en ? 8'd0 : "\n";
                    default: msg_byte = 0;
                endcase
                M_LED: case (i)
                    0: msg_byte = "l"; 1: msg_byte = "e"; 2: msg_byte = "d"; 3: msg_byte = " ";
                    4: msg_byte = hex({2'b00, led_mode}); 5: msg_byte = "\r"; 6: msg_byte = "\n";
                    default: msg_byte = 0;
                endcase
                default: msg_byte = 0;
            endcase
        end
    endfunction

    // pending requests, one bit per message
    reg [6:0] pend = 0;
    reg sending = 0;
    reg [2:0] cur = 0;
    reg [5:0] idx = 0;
    reg wait_busy = 0;

    always @(posedge clk) begin
        tx_start <= 0;
        if (rst) begin
            pend <= 7'b0000001;   // banner after configuration
            sending <= 0;
        end else begin
            if (tick && hb_en) pend[M_HB] <= 1;
            if (b_press[0]) pend[M_S1] <= 1;
            if (b_press[1]) pend[M_S2] <= 1;
            if (rx_valid) begin
                case (rx_byte)
                    "?", "i": pend[M_BANNER] <= 1;
                    "h": begin hb_en <= ~hb_en; pend[M_HBSTATE] <= 1; end
                    "l": begin led_mode <= (led_mode == 2) ? 2'd0 : led_mode + 1'b1; pend[M_LED] <= 1; end
                    default: if (!pend[M_ECHO]) begin echo_c <= rx_byte; pend[M_ECHO] <= 1; end
                endcase
            end

            if (!sending) begin
                // lowest index wins
                if      (pend[M_BANNER])  begin cur <= M_BANNER;  pend[M_BANNER]  <= 0; sending <= 1; end
                else if (pend[M_ECHO])    begin cur <= M_ECHO;    sending <= 1; end
                else if (pend[M_HBSTATE]) begin cur <= M_HBSTATE; pend[M_HBSTATE] <= 0; sending <= 1; end
                else if (pend[M_LED])     begin cur <= M_LED;     pend[M_LED]     <= 0; sending <= 1; end
                else if (pend[M_S1])      begin cur <= M_S1;      pend[M_S1]      <= 0; sending <= 1; end
                else if (pend[M_S2])      begin cur <= M_S2;      pend[M_S2]      <= 0; sending <= 1; end
                else if (pend[M_HB])      begin cur <= M_HB;      pend[M_HB]      <= 0; sending <= 1; cap <= secs; end
                idx <= 0;
                wait_busy <= 0;
            end else if (wait_busy) begin
                if (tx_busy) wait_busy <= 0;            // started; wait for it to finish below
            end else if (!tx_busy && !tx_start) begin
                if (msg_byte(cur, idx) == 0) begin
                    sending <= 0;
                    if (cur == M_ECHO) pend[M_ECHO] <= 0;  // echo_c held until sent
                end else begin
                    tx_data <= msg_byte(cur, idx);
                    tx_start <= 1;
                    idx <= idx + 1'b1;
                    wait_busy <= 1;
                end
            end
        end
    end

    // ---------------- LEDs ----------------
    reg [22:0] chase_t = 0;
    reg [5:0] chase = 6'b000001;
    always @(posedge clk) begin
        chase_t <= chase_t + 1'b1;
        if (chase_t == 0) chase <= {chase[4:0], chase[5]};
    end
    reg [5:0] led;
    always @(*) case (led_mode)
        2'd0: led = secs[5:0];
        2'd1: led = chase;
        default: led = 6'b0;
    endcase
    assign led_n = ~led;
endmodule
