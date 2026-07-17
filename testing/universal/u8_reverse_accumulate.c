long long results[3];
int main(){
    int a[5]={10,20,30,40,50}; int *lo=a; int *hi=a+4; int t;
    while(lo<hi){ t=*lo; *lo=*hi; *hi=t; lo++; hi--; }   /* reverse in place */
    long long s=0; int i; for(i=0;i<5;i++) s+=a[i]*(i+1);
    results[0]=a[0]; results[1]=a[4]; results[2]=s;  /* 50,10,... */
    return 1;
}
