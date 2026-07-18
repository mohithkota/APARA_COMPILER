long long results[3];
int main(){
    results[0]=(int)5.0f;          /* literal -> int cast only */
    float a=3.5f;
    results[1]=(int)a;             /* var load + cast */
    results[2]=(int)(3.5f+1.5f);   /* add + cast */
    return 1;
}
