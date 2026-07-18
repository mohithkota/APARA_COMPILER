long long results[4];
float garr[3]={1.5f,2.5f,4.0f};
float gs=0.5f;
double dg=1.25;
int main(){
    results[0]=(int)(garr[0]+garr[1]);  /* 4  global float array init + arith */
    garr[2]=garr[2]*2.0f;
    results[1]=(int)garr[2];            /* 8  store back to float array */
    results[2]=(int)(gs*8.0f);          /* 4  float global scalar */
    results[3]=(int)(dg*4.0);           /* 5  double global scalar */
    return 1;
}
