/* test_logic.c — tests NOR (~|), NAND (~&), XNOR (~~) via __nor/__nand/__xnor,
   and NOP via __nop */

int g_nor  = 0;
int g_nand = 0;
int g_xnor = 0;

/* Compiler intrinsics — no declaration needed; handled by codegen */
int __nor (int a, int b);
int __nand(int a, int b);
int __xnor(int a, int b);
void __nop(void);

int main() {
    int a = 0xF0;   /* 11110000 */
    int b = 0x0F;   /* 00001111 */

    __nop();                     /* $nop              */
    g_nor  = __nor (a, b);       /* ~(a | b) = 0      */
    g_nand = __nand(a, b);       /* ~(a & b) = ~0 = -1 */
    g_xnor = __xnor(a, b);       /* ~(a ^ b)          */

    return g_nor + g_nand + g_xnor;
}
