long long results[6];
int main(){
    int a[6]={5,2,9,1,7,3}; int i,j,t;
    for(i=0;i<6;i++) for(j=0;j<5-i;j++) if(a[j]>a[j+1]){t=a[j];a[j]=a[j+1];a[j+1]=t;}
    for(i=0;i<6;i++) results[i]=a[i];   /* sorted: 1 2 3 5 7 9 */
    return 1;
}
