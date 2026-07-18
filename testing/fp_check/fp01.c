long long results[4];
int main(){
    results[0]=(int)(3.5f+1.5f);    /* 5  */
    results[1]=(int)(10.0f-2.5f);   /* 7  */
    results[2]=(int)(3.0f*4.0f);    /* 12 */
    results[3]=(int)(20.0f/8.0f);   /* 2  (20/8=2.5 -> (int)2) */
    return 1;
}
