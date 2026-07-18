float fresults[4];
double dresults[2];
int main(){
    float a=1.5f, b=2.25f;
    fresults[0]=a+b;       /* 3.75  = 0x40700000 */
    fresults[1]=a*b;       /* 3.375 = 0x40580000 */
    fresults[2]=-a;        /* -1.5  = 0xBFC00000 */
    fresults[3]=a/2.0f;    /* 0.75  = 0x3F400000 */
    double d=1.1;
    dresults[0]=d*3.0;     /* bit-exact vs gcc (rounding included) */
    dresults[1]=d-0.1;
    return 1;
}
