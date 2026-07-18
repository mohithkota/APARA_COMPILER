long long results[4];
float half(float x){ return x/2.0f; }
float fmax2(float a, float b){ if(a>b) return a; return b; }
double dscale(double d, int k){ return d*k; }
int main(){
    results[0]=(int)half(9.0f);              /* 4  (4.5) */
    results[1]=(int)fmax2(1.5f,2.5f);        /* 2  */
    results[2]=(int)(half(5.0f)+half(3.0f)); /* 4  (2.5+1.5) */
    results[3]=(int)dscale(2.5,4);           /* 10 */
    return 1;
}
