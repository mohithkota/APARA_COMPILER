struct P { int x; int y; };
int g2d[3][3] = {{1,2,3},{4,5,6},{7,8,9}};
int arr[6] = {10,20,30,40,50,60};
long long results[10];
int sumarr(int *p, int n){ int s=0,i; for(i=0;i<n;i++) s+=p[i]; return s; }
int main(){
    struct P a; a.x=7; a.y=11;
    int *p = arr;
    results[0]=arr[0]+arr[5];      /* 70 */
    results[1]=g2d[1][1];          /* 5 */
    results[2]=g2d[2][0]+g2d[0][2];/* 10 */
    results[3]=a.x*a.y;            /* 77 */
    results[4]=*(p+3);             /* 40 */
    results[5]=p[2]-p[0];          /* 20 */
    results[6]=sumarr(arr,6);      /* 210 */
    results[7]=sumarr(arr+2,3);    /* 120 */
    int i,ss=0; for(i=0;i<3;i++){int j; for(j=0;j<3;j++) ss+=g2d[i][j];}
    results[8]=ss;                 /* 45 */
    a.x += a.y; results[9]=a.x;    /* 18 */
    return 1;
}
