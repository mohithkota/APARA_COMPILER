int m[3][3]={{1,2,3},{4,5,6},{7,8,9}}; long long results[9];
int main(){
    int i,j,k=0; for(i=0;i<3;i++) for(j=0;j<3;j++) results[k++]=m[j][i]; /* transposed */
    return 1;
}
