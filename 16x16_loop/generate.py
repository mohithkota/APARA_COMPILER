with open('16x16.result', 'w') as f:
    mem_addr = 0x40 # Base of Matrix C (Byte 512)

    for r in range(16):
        f.write(f"// --- Row {r+1} Results ---\n")
        for c in range(16):
            dot_sum = 0
            for k in range(16):
                # This performs true matrix multiplication and mimics the 8-bit wrap
                a_val = (r * 16 + k + 1) % 256
                b_val = (k * 16 + c + 1) % 256

                dot_sum += a_val * b_val

            hex_val = dot_sum & 0xFFFFFFFFFFFFFFFF
            f.write(f"mem {hex(mem_addr)} 0x{hex_val:016x}\n")
            mem_addr += 1
        f.write("\n")

print("Perfect 256-line 16x16.result generated!")
